from __future__ import annotations

import json
import logging
import os
import re
import subprocess
import sys
import tempfile
import urllib.error
import urllib.request
from dataclasses import dataclass
from pathlib import Path

import click
from manifest_builder import generate


GITHUB_PROXY_AUDIENCE = "idcat.noa.re"
GITHUB_PROXY_BASE_URL = "https://idcat.noa.re/proxy"
GITHUB_API_VERSION = "2026-03-10"
MAX_COMMENT_CHARS = 65_536
FULL_DIFF_ARTIFACT = "manifest-builder.diff"
MANIFEST_CONFIG_DIR = Path(".")

logger = logging.getLogger("diffcomment")


@dataclass(frozen=True)
class ManifestDiff:
    stat: str
    diff: str


@dataclass(frozen=True)
class CommentBody:
    body: str
    omitted_context_diff: bool


@click.command()
@click.option(
    "--target-repository",
    help="Repository to shallow-clone as the manifest output before diff generation.",
)
@click.option(
    "--dump",
    is_flag=True,
    help="Write the generated GitHub comment body to stdout instead of posting it.",
)
def main(target_repository: str | None, dump: bool) -> None:
    logging.basicConfig(
        format="%(asctime)s %(levelname)s %(message)s",
        datefmt="%Y-%m-%d %H:%M:%S",
        level=logging.INFO,
        stream=sys.stderr,
        force=True,
    )

    pr_number = os.environ.get("BUILDKITE_PULL_REQUEST")
    if not pr_number or pr_number == "false":
        if dump:
            pr_number = "local"
        else:
            logger.info(
                "skipping manifest diff comment because this build was not triggered by a pull request"
            )
            return

    if not target_repository:
        raise click.UsageError(
            "diffcomment requires --target-repository for pull requests"
        )

    logger.info("running manifest-builder diff for pull request #%s", pr_number)
    returncode, diff = run_manifest_builder_diff(target_repository)
    logger.info("manifest-builder diff exited with code %s", returncode)

    comment = build_comment_body(pr_number, returncode, diff)
    if dump:
        logger.info(
            "writing manifest diff comment body to stdout instead of posting to GitHub"
        )
        click.echo(comment.body)
        raise click.exceptions.Exit(returncode)

    if comment.omitted_context_diff:
        write_full_diff_artifact(Path.cwd(), diff)
        upload_full_diff_artifact(Path.cwd())

    owner, repo = github_repo()
    logger.info("requesting GitHub API proxy token for GitHub comment")
    token = request_github_proxy_token()

    logger.info(
        "posting manifest diff comment to %s/%s pull request #%s",
        owner,
        repo,
        pr_number,
    )
    post_issue_comment(token, owner, repo, pr_number, comment.body)
    logger.info("posted manifest diff comment")

    raise click.exceptions.Exit(returncode)


def run_manifest_builder_diff(
    target_repository: str,
    manifest_config_dir: Path = MANIFEST_CONFIG_DIR,
) -> tuple[int, ManifestDiff]:
    with tempfile.TemporaryDirectory(prefix="bktools-diffcomment-") as tmpdir:
        output_dir = Path(tmpdir) / "output"
        clone_target_repository(target_repository, output_dir)
        generate(manifest_config_dir, output_dir)
        diff_stat = git_diff_stat(output_dir)
        diff_output = git_diff(output_dir)

    return 0, ManifestDiff(stat=diff_stat, diff=diff_output)


def clone_target_repository(target_repository: str, output_dir: Path) -> None:
    logger.info("cloning target repository %s to %s", target_repository, output_dir)
    subprocess.run(
        ["git", "clone", "--depth", "1", target_repository, str(output_dir)],
        check=True,
    )


def git_diff_stat(output_dir: Path) -> str:
    result = subprocess.run(
        ["git", "diff", "--stat"],
        cwd=output_dir,
        text=True,
        capture_output=True,
        check=True,
    )
    return result.stdout


def git_diff(output_dir: Path) -> str:
    result = subprocess.run(
        ["git", "diff"],
        cwd=output_dir,
        text=True,
        capture_output=True,
        check=True,
    )
    return result.stdout


def write_full_diff_artifact(repo_root: Path, diff: ManifestDiff) -> Path:
    artifact_path = repo_root / FULL_DIFF_ARTIFACT
    logger.info("writing full manifest diff to %s", artifact_path)
    artifact_path.write_text(render_full_diff_artifact(diff))
    return artifact_path


def render_full_diff_artifact(diff: ManifestDiff) -> str:
    if not diff.stat.strip() and not diff.diff.strip():
        return "The generated output is the same before and after this change\n"

    sections = []
    if diff.stat.strip():
        sections.append(diff.stat.strip())
    if diff.diff.strip():
        sections.append(diff.diff.strip())
    return "\n\n".join(sections) + "\n"


def upload_full_diff_artifact(repo_root: Path) -> None:
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


def request_github_proxy_token() -> str:
    audience = os.environ.get("BKTOOLS_GITHUB_PROXY_AUDIENCE", GITHUB_PROXY_AUDIENCE)
    return subprocess.check_output(
        ["buildkite-agent", "oidc", "request-token", "--audience", audience],
        text=True,
    ).strip()


def build_comment_body(
    pr_number: str, returncode: int, diff: ManifestDiff
) -> CommentBody:
    build_url = os.environ.get("BUILDKITE_BUILD_URL")
    commit = os.environ.get("BUILDKITE_COMMIT")

    del pr_number

    lines = ["### `manifest-builder diff`"]
    metadata = []

    if build_url:
        metadata.append(f"Build: {build_url}")
    if commit:
        metadata.append(f"Commit: `{commit}`")
    if returncode:
        metadata.append(f"Exit code: `{returncode}`")

    if metadata:
        lines.extend(["", *metadata])

    stat = diff.stat.strip()
    context_diff = diff.diff.strip()
    if not stat and not context_diff:
        lines.extend(
            ["", "The generated output is the same before and after this change"]
        )
        return CommentBody(body="\n".join(lines), omitted_context_diff=False)

    if stat:
        stat_fence = markdown_fence(stat)
        lines.extend(["", stat_fence, stat, stat_fence])

    body_with_context = "\n".join(
        [
            *lines,
            "",
            f"{markdown_fence(context_diff)}diff",
            context_diff,
            markdown_fence(context_diff),
        ]
    )
    if len(body_with_context) <= MAX_COMMENT_CHARS:
        return CommentBody(body=body_with_context, omitted_context_diff=False)

    body_without_context = "\n".join(
        [
            *lines,
            "",
            (
                "_The full context diff is too large for a GitHub comment and "
                f"has been uploaded as Buildkite artifact `{FULL_DIFF_ARTIFACT}`._"
            ),
        ]
    )
    return CommentBody(body=body_without_context, omitted_context_diff=True)


def markdown_fence(text: str) -> str:
    longest_backtick_run = max(
        (len(match.group(0)) for match in re.finditer(r"`+", text)), default=0
    )
    return "`" * max(3, longest_backtick_run + 1)


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
