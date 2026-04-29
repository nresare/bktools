from __future__ import annotations

import json
import logging
import os
import re
import subprocess
import sys
import urllib.error
import urllib.request
from contextlib import redirect_stderr, redirect_stdout
from io import StringIO

from manifest_builder.cli import main as manifest_builder_main


AUDIENCE = "idcat.noa.re"
IDCAT_BASE_URL = "https://idcat.noa.re/proxy"
GITHUB_API_VERSION = "2026-03-10"
MAX_COMMENT_BYTES = 60_000

logger = logging.getLogger("diffcomment")


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

    logger.info("running manifest-builder --diff for pull request #%s", pr_number)
    returncode, output = run_manifest_builder_diff()
    logger.info("manifest-builder --diff exited with code %s", returncode)

    owner, repo = github_repo()
    logger.info("requesting idcat token for GitHub comment")
    token = request_idcat_token()

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


def run_manifest_builder_diff() -> tuple[int, str]:
    output = StringIO()
    try:
        with redirect_stdout(output), redirect_stderr(output):
            result = manifest_builder_main(args=["--diff"], standalone_mode=False)
    except SystemExit as error:
        returncode = int(error.code) if isinstance(error.code, int) else 1
    else:
        returncode = int(result) if isinstance(result, int) else 0

    return returncode, output.getvalue()


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


def request_idcat_token() -> str:
    return subprocess.check_output(
        ["buildkite-agent", "oidc", "request-token", "--audience", AUDIENCE],
        text=True,
    ).strip()


def build_comment_body(pr_number: str, returncode: int, output: str) -> str:
    build_url = os.environ.get("BUILDKITE_BUILD_URL")
    commit = os.environ.get("BUILDKITE_COMMIT")

    lines = [
        "### `manifest-builder --diff`",
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
    url = (
        f"{IDCAT_BASE_URL}/nresare-buildsystem/repos/{owner}/{repo}"
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
