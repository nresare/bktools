from __future__ import annotations

import json
import logging
import os
import re
import subprocess
import sys
import tempfile
import tomllib
import urllib.error
import urllib.request
from collections.abc import Callable
from contextlib import redirect_stderr, redirect_stdout
from dataclasses import dataclass
from importlib import import_module
from io import StringIO
from pathlib import Path
from typing import cast


GITHUB_PROXY_AUDIENCE = "github-api-proxy.noa.re"
GITHUB_PROXY_BASE_URL = "https://github-api-proxy.noa.re/proxy"
GITHUB_API_VERSION = "2026-03-10"
MAX_COMMENT_BYTES = 60_000
PIPELINEGEN_CONFIG = Path(".buildkite/pipelinegen.toml")
MANIFEST_CONFIG_DIR = Path("conf")

logger = logging.getLogger("diffcomment")


@dataclass(frozen=True)
class DiffcommentConfig:
    target_repository: str


def main() -> int:
    logging.basicConfig(
        format="%(asctime)s %(levelname)s %(message)s",
        datefmt="%Y-%m-%d %H:%M:%S",
        level=logging.INFO,
        stream=sys.stderr,
        force=True,
    )

    pr_number = os.environ.get("BUILDKITE_PULL_REQUEST")
    if not pr_number or pr_number == "false":
        logger.info(
            "skipping manifest diff comment because this build was not triggered by a pull request"
        )
        return 0

    logger.info("running manifest-builder diff for pull request #%s", pr_number)
    returncode, output = run_manifest_builder_diff()
    logger.info("manifest-builder diff exited with code %s", returncode)

    owner, repo = github_repo()
    logger.info("requesting GitHub API proxy token for GitHub comment")
    token = request_github_proxy_token()

    body = build_comment_body(pr_number, returncode, output)
    logger.info(
        "posting manifest diff comment to %s/%s pull request #%s",
        owner,
        repo,
        pr_number,
    )
    post_issue_comment(token, owner, repo, pr_number, body)
    logger.info("posted manifest diff comment")

    return returncode


def run_manifest_builder_diff(
    config_path: Path = PIPELINEGEN_CONFIG,
    manifest_config_dir: Path = MANIFEST_CONFIG_DIR,
) -> tuple[int, str]:
    output = StringIO()
    try:
        with redirect_stdout(output), redirect_stderr(output):
            result = run_manifest_builder_show_diff(config_path, manifest_config_dir)
    except SystemExit as error:
        returncode = int(error.code) if isinstance(error.code, int) else 1
    else:
        returncode = int(result) if isinstance(result, int) else 0

    return returncode, output.getvalue()


def run_manifest_builder_show_diff(
    config_path: Path = PIPELINEGEN_CONFIG,
    manifest_config_dir: Path = MANIFEST_CONFIG_DIR,
) -> int:
    config = read_diffcomment_config(config_path)
    with tempfile.TemporaryDirectory(prefix="bktools-diffcomment-") as tmpdir:
        output_dir = Path(tmpdir) / "output"
        clone_target_repository(config.target_repository, output_dir)
        diff_output = manifest_builder_show_diff(manifest_config_dir, output_dir)

    if diff_output:
        print(diff_output, end="")
    else:
        print("The output is identical before and after this change")
    return 0


def read_diffcomment_config(config_path: Path) -> DiffcommentConfig:
    try:
        config = tomllib.loads(config_path.read_text())
    except FileNotFoundError as error:
        raise SystemExit(f"diffcomment config not found: {config_path}") from error
    except tomllib.TOMLDecodeError as error:
        raise SystemExit(
            f"failed to parse diffcomment config {config_path}: {error}"
        ) from error

    entries = config.get("diffcomment")
    if not isinstance(entries, list) or len(entries) != 1:
        raise SystemExit(
            f"diffcomment config {config_path} must contain exactly one [[diffcomment]] table"
        )

    target_repository = entries[0].get("target_repository")
    if not isinstance(target_repository, str) or not target_repository:
        raise SystemExit(
            f"diffcomment config {config_path} [[diffcomment]] must contain "
            "string key 'target_repository'"
        )

    return DiffcommentConfig(target_repository=target_repository)


def clone_target_repository(target_repository: str, output_dir: Path) -> None:
    logger.info("cloning target repository %s to %s", target_repository, output_dir)
    subprocess.run(
        ["git", "clone", "--depth", "1", target_repository, str(output_dir)],
        check=True,
    )


def manifest_builder_show_diff(config: Path, output: Path) -> str:
    module = import_module("manifest_builder.cli")
    show_diff = cast(Callable[[Path, Path], str], getattr(module, "show_diff"))

    return show_diff(config, output)


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


def build_comment_body(pr_number: str, returncode: int, output: str) -> str:
    build_url = os.environ.get("BUILDKITE_BUILD_URL")
    commit = os.environ.get("BUILDKITE_COMMIT")

    lines = [
        "### `manifest-builder diff`",
        "",
        f"Pull request: #{pr_number}",
    ]

    if build_url:
        lines.append(f"Build: {build_url}")
    if commit:
        lines.append(f"Commit: `{commit}`")
    if returncode:
        lines.append(f"Exit code: `{returncode}`")

    diff_output = output.strip() or "No diff output produced."
    fence = markdown_fence(diff_output)
    lines.extend(["", f"{fence}diff", diff_output, fence])

    return truncate_comment("\n".join(lines))


def markdown_fence(text: str) -> str:
    longest_backtick_run = max(
        (len(match.group(0)) for match in re.finditer(r"`+", text)), default=0
    )
    return "`" * max(3, longest_backtick_run + 1)


def truncate_comment(body: str) -> str:
    encoded = body.encode()
    if len(encoded) <= MAX_COMMENT_BYTES:
        return body

    suffix = "\n\n_Output truncated to fit within the GitHub comment size limit._"
    allowed_bytes = MAX_COMMENT_BYTES - len(suffix.encode())
    truncated = encoded[:allowed_bytes].decode(errors="ignore").rstrip()
    return f"{truncated}{suffix}"


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
    raise SystemExit(main())
