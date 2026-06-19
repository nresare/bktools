from __future__ import annotations

import json
import logging
import os
import re
import subprocess
import sys
import urllib.error
import urllib.parse
import urllib.request
from collections.abc import Mapping
from dataclasses import dataclass
from pathlib import Path

import click
from bktools.manifest_builder_on_checkout import run_manifest_builder_on_checkout
from bktools.manifest_diff import (
    FULL_DIFF_ARTIFACT,
    ManifestDiff,
    build_comment_body,
    render_full_diff_artifact,
    run_manifest_builder_diff,
)


GITHUB_PROXY_AUDIENCE = "idcat.noa.re"
GITHUB_PROXY_BASE_URL = "https://idcat.noa.re/proxy"
GITHUB_API_VERSION = "2026-03-10"

logger = logging.getLogger("diffcomment")
CiSystem = str


@dataclass(frozen=True)
class CiContext:
    pull_request_number: str | None
    owner: str | None = None
    repo: str | None = None


@click.command()
@click.option(
    "--input",
    "input_dir",
    type=click.Path(exists=True, file_okay=False, path_type=Path),
    help="Generated manifest output checkout to diff.",
)
@click.option(
    "--target-repo",
    help="Repository to shallow-clone as the manifest output before generation.",
)
@click.option(
    "--dump",
    is_flag=True,
    help="Write the generated GitHub comment body to stdout instead of posting it.",
)
@click.option(
    "--ci-system",
    type=click.Choice(["buildkite", "github"]),
    default="buildkite",
    show_default=True,
    help="CI system environment to read.",
)
def main(
    input_dir: Path | None, target_repo: str | None, dump: bool, ci_system: CiSystem
) -> None:
    logging.basicConfig(
        format="%(asctime)s %(levelname)s %(message)s",
        datefmt="%Y-%m-%d %H:%M:%S",
        level=logging.INFO,
        stream=sys.stderr,
        force=True,
    )

    ci_context = read_ci_context(ci_system)
    pr_number = ci_context.pull_request_number
    if pr_number is None:
        if dump:
            pr_number = "local"
        else:
            logger.info(
                "skipping manifest diff comment because this build was not triggered by a pull request"
            )
            return

    input_dir = prepare_manifest_dir(input_dir, target_repo)

    logger.info("running manifest-builder diff for pull request #%s", pr_number)
    returncode, diff = run_manifest_builder_diff(input_dir)
    logger.info("manifest-builder diff exited with code %s", returncode)

    comment = build_comment_body(
        pr_number, returncode, diff, full_diff_reference=full_diff_reference(ci_system)
    )
    if dump:
        logger.info(
            "writing manifest diff comment body to stdout instead of posting to GitHub"
        )
        click.echo(comment.body)
        raise click.exceptions.Exit(returncode)

    if comment.omitted_context_diff:
        write_full_diff_artifact(Path.cwd(), diff)
        upload_full_diff_artifact(Path.cwd(), ci_system=ci_system)

    owner, repo_name = ci_context.owner, ci_context.repo
    if (owner is None or repo_name is None) and ci_system == "buildkite":
        owner, repo_name = github_repo()
    if owner is None or repo_name is None:
        raise click.ClickException(
            f"could not infer GitHub repository from {ci_system} environment"
        )
    logger.info("requesting GitHub API proxy token for GitHub comment")
    token = request_github_proxy_token(ci_system)

    logger.info(
        "posting manifest diff comment to %s/%s pull request #%s",
        owner,
        repo_name,
        pr_number,
    )
    post_issue_comment(token, owner, repo_name, pr_number, comment.body)
    logger.info("posted manifest diff comment")

    raise click.exceptions.Exit(returncode)


def prepare_manifest_dir(input_dir: Path | None, target_repo: str | None) -> Path:
    if input_dir is not None and target_repo is not None:
        raise click.UsageError(
            "diffcomment accepts only one of --input or --target-repo"
        )
    if input_dir is not None:
        return input_dir
    if target_repo is not None:
        return run_manifest_builder_on_checkout(target_repo, create_commit=False)
    raise click.UsageError("diffcomment requires --input or --target-repo")


def read_ci_context(ci_system: CiSystem) -> CiContext:
    if ci_system == "buildkite":
        return read_buildkite_context()
    if ci_system == "github":
        return read_github_actions_context()
    raise ValueError(f"unsupported CI system: {ci_system}")


def read_buildkite_context() -> CiContext:
    pr_number = os.environ.get("BUILDKITE_PULL_REQUEST")
    if not pr_number or pr_number == "false":
        return CiContext(pull_request_number=None)
    return CiContext(pull_request_number=pr_number)


def read_github_actions_context() -> CiContext:
    if os.environ.get("GITHUB_EVENT_NAME") not in (
        "pull_request",
        "pull_request_target",
    ):
        return CiContext(pull_request_number=None)

    event = read_github_event()
    pr_number = event.get("number")
    if not isinstance(pr_number, int | str):
        raise click.ClickException(
            "could not infer pull request number from GitHub event"
        )

    full_name = os.environ.get("GITHUB_REPOSITORY") or github_event_repository(event)
    owner, repo = parse_github_repository_name(full_name)
    return CiContext(
        pull_request_number=str(pr_number),
        owner=owner,
        repo=repo,
    )


def read_github_event() -> dict[str, object]:
    event_path = os.environ.get("GITHUB_EVENT_PATH")
    if not event_path:
        raise click.ClickException(
            "GITHUB_EVENT_PATH is required for --ci-system=github"
        )

    event = json.loads(Path(event_path).read_text())
    if not isinstance(event, dict):
        raise click.ClickException("GitHub event payload must be a JSON object")
    return event


def github_event_repository(event: Mapping[str, object]) -> str:
    repository = event.get("repository")
    if not isinstance(repository, Mapping):
        raise click.ClickException(
            "could not infer GitHub repository from event payload"
        )

    full_name = repository.get("full_name")
    if not isinstance(full_name, str) or not full_name:
        raise click.ClickException(
            "could not infer GitHub repository from event payload"
        )
    return full_name


def parse_github_repository_name(full_name: str) -> tuple[str, str]:
    parts = full_name.split("/", 1)
    if len(parts) != 2 or not parts[0] or not parts[1]:
        raise click.ClickException(f"invalid GitHub repository name: {full_name!r}")
    return parts[0], parts[1]


def write_full_diff_artifact(repo_root: Path, diff: ManifestDiff) -> Path:
    artifact_path = repo_root / FULL_DIFF_ARTIFACT
    logger.info("writing full manifest diff to %s", artifact_path)
    artifact_path.write_text(render_full_diff_artifact(diff))
    return artifact_path


def upload_full_diff_artifact(
    repo_root: Path, ci_system: CiSystem = "buildkite"
) -> None:
    if ci_system == "github":
        logger.info(
            "leaving full manifest diff artifact %s for GitHub Actions upload",
            repo_root / FULL_DIFF_ARTIFACT,
        )
        return
    if ci_system != "buildkite":
        raise ValueError(f"unsupported CI system: {ci_system}")

    logger.info("uploading full manifest diff artifact %s", FULL_DIFF_ARTIFACT)
    subprocess.run(
        ["buildkite-agent", "artifact", "upload", FULL_DIFF_ARTIFACT],
        cwd=repo_root,
        check=True,
    )


def github_repo() -> tuple[str, str]:
    repo_url = os.environ.get("BUILDKITE_PULL_REQUEST_REPO") or os.environ.get(
        "BUILDKITE_REPO", ""
    )
    match = re.search(r"github\.com[:/]([^/]+)/([^/]+?)(?:\.git)?$", repo_url)
    if not match:
        raise RuntimeError(
            f"could not infer GitHub owner/repo from Buildkite repo URL: {repo_url!r}"
        )
    return match.group(1), match.group(2)


def request_github_proxy_token(ci_system: CiSystem = "buildkite") -> str:
    if ci_system == "buildkite":
        return request_buildkite_github_proxy_token()
    if ci_system == "github":
        return request_github_actions_proxy_token()
    raise ValueError(f"unsupported CI system: {ci_system}")


def request_buildkite_github_proxy_token() -> str:
    audience = os.environ.get("BKTOOLS_GITHUB_PROXY_AUDIENCE", GITHUB_PROXY_AUDIENCE)
    return subprocess.check_output(
        ["buildkite-agent", "oidc", "request-token", "--audience", audience],
        text=True,
    ).strip()


def request_github_actions_proxy_token() -> str:
    audience = os.environ.get("BKTOOLS_GITHUB_PROXY_AUDIENCE", GITHUB_PROXY_AUDIENCE)
    request_url = os.environ.get("ACTIONS_ID_TOKEN_REQUEST_URL")
    request_token = os.environ.get("ACTIONS_ID_TOKEN_REQUEST_TOKEN")
    if not request_url or not request_token:
        raise click.ClickException(
            "GitHub Actions OIDC token environment is required for --ci-system=github"
        )

    separator = "&" if "?" in request_url else "?"
    url = f"{request_url}{separator}{urllib.parse.urlencode({'audience': audience})}"
    request = urllib.request.Request(
        url,
        headers={
            "Authorization": f"Bearer {request_token}",
            "Accept": "application/json",
        },
    )
    with urllib.request.urlopen(request, timeout=30) as response:
        payload = json.loads(response.read())

    value = payload.get("value") if isinstance(payload, dict) else None
    if not isinstance(value, str) or not value:
        raise click.ClickException(
            "GitHub Actions OIDC response did not contain a token"
        )
    return value


def full_diff_reference(ci_system: CiSystem) -> str:
    if ci_system == "buildkite":
        return f"uploaded as Buildkite artifact `{FULL_DIFF_ARTIFACT}`"
    if ci_system == "github":
        return (
            f"written to `{FULL_DIFF_ARTIFACT}` for upload as a GitHub Actions artifact"
        )
    raise ValueError(f"unsupported CI system: {ci_system}")


def post_issue_comment(
    token: str, owner: str, repo: str, pr_number: str, body: str
) -> None:
    base_url = os.environ.get("BKTOOLS_GITHUB_PROXY_BASE_URL", GITHUB_PROXY_BASE_URL)
    url = (
        f"{base_url}/nresare-buildsystem/repos/{owner}/{repo}"
        f"/issues/{pr_number}/comments"
    )
    request = urllib.request.Request(
        url,
        data=json.dumps({"body": body}).encode(),
        headers={
            "Authorization": f"Bearer {token}",
            "Accept": "application/vnd.github+json",
            "Content-Type": "application/json",
            "X-GitHub-Api-Version": GITHUB_API_VERSION,
        },
        method="POST",
    )

    try:
        with urllib.request.urlopen(request, timeout=30) as response:
            response.read()
    except urllib.error.HTTPError as error:
        logger.error(error.read().decode(errors="replace"))
        raise


if __name__ == "__main__":
    main()
