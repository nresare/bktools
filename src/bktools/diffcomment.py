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
from collections import Counter
from collections.abc import Mapping
from dataclasses import dataclass
from pathlib import Path
from typing import cast

import click
import yaml
from bktools.manifest_builder_on_checkout import run_manifest_builder_on_checkout
from manifest_builder import __version__ as MANIFEST_BUILDER_VERSION


GITHUB_PROXY_AUDIENCE = "idcat.noa.re"
GITHUB_PROXY_BASE_URL = "https://idcat.noa.re/proxy"
GITHUB_API_VERSION = "2026-03-10"
MAX_COMMENT_CHARS = 65_536
FULL_DIFF_ARTIFACT = "manifest-builder.diff"
DEPLOY_ID_METADATA_PATH = ("metadata", "annotations", "noa.re/deploy-id")
METADATA_SUMMARY_THRESHOLD = 2

logger = logging.getLogger("diffcomment")
CiSystem = str


@dataclass(frozen=True)
class ManifestDiff:
    stat: str
    diff: str
    summary: str = ""
    filtered_diff: str | None = None


@dataclass(frozen=True)
class CommentBody:
    body: str
    omitted_context_diff: bool


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

    input_dir = resolve_input_dir(input_dir, target_repo)

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


def resolve_input_dir(input_dir: Path | None, target_repo: str | None) -> Path:
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


def run_manifest_builder_diff(
    input_dir: Path,
) -> tuple[int, ManifestDiff]:
    git_add_all(input_dir)
    diff_stat = git_diff_stat(input_dir)
    diff_output = git_diff(input_dir)
    summary, filtered_diff = smart_manifest_diff(input_dir, diff_output)

    return 0, ManifestDiff(
        stat=diff_stat,
        diff=diff_output,
        summary=summary,
        filtered_diff=filtered_diff,
    )


def git_add_all(output_dir: Path) -> None:
    subprocess.run(
        ["git", "add", "."],
        cwd=output_dir,
        check=True,
    )


def git_diff_stat(output_dir: Path) -> str:
    result = subprocess.run(
        ["git", "diff", "--cached", "--stat"],
        cwd=output_dir,
        text=True,
        capture_output=True,
        check=True,
    )
    return result.stdout


def git_diff(output_dir: Path) -> str:
    result = subprocess.run(
        ["git", "diff", "--cached"],
        cwd=output_dir,
        text=True,
        capture_output=True,
        check=True,
    )
    return result.stdout


def git_diff_name_only(output_dir: Path) -> list[str]:
    result = subprocess.run(
        ["git", "diff", "--cached", "--name-only"],
        cwd=output_dir,
        text=True,
        capture_output=True,
        check=True,
    )
    return [line for line in result.stdout.splitlines() if line]


def git_show(output_dir: Path, revision_path: str) -> str | None:
    result = subprocess.run(
        ["git", "show", revision_path],
        cwd=output_dir,
        text=True,
        capture_output=True,
    )
    if result.returncode:
        return None
    return result.stdout


def smart_manifest_diff(output_dir: Path, raw_diff: str) -> tuple[str, str | None]:
    try:
        metadata_changes = summarize_metadata_changes(output_dir)
    except (subprocess.SubprocessError, yaml.YAMLError, OSError):
        logger.exception("failed to summarize manifest metadata changes")
        return "", None

    summary_paths = {
        change.path
        for change, count in metadata_changes.items()
        if count >= METADATA_SUMMARY_THRESHOLD
    }
    suppress_paths = {DEPLOY_ID_METADATA_PATH, *summary_paths}
    if not suppress_paths:
        return "", None

    filtered_diff = filter_metadata_hunks(raw_diff, suppress_paths)
    summary = render_metadata_summary(metadata_changes)
    if filtered_diff == raw_diff and not summary:
        return "", None
    return summary, filtered_diff


@dataclass(frozen=True)
class MetadataChange:
    section: str
    key: str
    old: str | None
    new: str | None

    @property
    def path(self) -> tuple[str, str, str]:
        return ("metadata", self.section, self.key)


def summarize_metadata_changes(output_dir: Path) -> Counter[MetadataChange]:
    changes: Counter[MetadataChange] = Counter()
    for path in git_diff_name_only(output_dir):
        if not path.endswith((".yaml", ".yml")):
            continue
        old_content = git_show(output_dir, f"HEAD:{path}")
        new_content = git_show(output_dir, f":{path}")
        if new_content is None:
            continue
        changes.update(compare_manifest_metadata(old_content or "", new_content))
    return changes


def compare_manifest_metadata(
    old_content: str, new_content: str
) -> Counter[MetadataChange]:
    old_docs = parse_yaml_documents(old_content)
    new_docs = parse_yaml_documents(new_content)
    changes: Counter[MetadataChange] = Counter()
    for index, new_doc in enumerate(new_docs):
        old_doc = old_docs[index] if index < len(old_docs) else {}
        changes.update(compare_document_metadata(old_doc, new_doc))
    return changes


def parse_yaml_documents(content: str) -> list[object]:
    if not content.strip():
        return []
    return list(yaml.safe_load_all(content))


def compare_document_metadata(
    old_doc: object, new_doc: object
) -> Counter[MetadataChange]:
    changes: Counter[MetadataChange] = Counter()
    old_metadata = mapping_value(old_doc, "metadata")
    new_metadata = mapping_value(new_doc, "metadata")
    for section in ("labels", "annotations"):
        old_values = string_mapping_value(old_metadata, section)
        new_values = string_mapping_value(new_metadata, section)
        for key in old_values.keys() | new_values.keys():
            path = ("metadata", section, key)
            if path == DEPLOY_ID_METADATA_PATH:
                continue
            old = old_values.get(key)
            new = new_values.get(key)
            if old != new:
                changes[MetadataChange(section, key, old, new)] += 1
    return changes


def mapping_value(value: object, key: str) -> dict[object, object]:
    if not isinstance(value, Mapping):
        return {}
    mapping = cast(Mapping[object, object], value)
    child = mapping.get(key)
    if not isinstance(child, Mapping):
        return {}
    child_mapping = cast(Mapping[object, object], child)
    return {child_key: child_value for child_key, child_value in child_mapping.items()}


def string_mapping_value(value: object, key: str) -> dict[str, str]:
    mapping = mapping_value(value, key)
    return {
        str(child_key): str(child_value) for child_key, child_value in mapping.items()
    }


def render_metadata_summary(metadata_changes: Counter[MetadataChange]) -> str:
    lines = []
    for change, count in sorted(
        metadata_changes.items(),
        key=lambda item: (
            item[0].section,
            item[0].key,
            item[0].old or "",
            item[0].new or "",
        ),
    ):
        if count < METADATA_SUMMARY_THRESHOLD:
            continue
        name = "Label" if change.section == "labels" else "Annotation"
        if change.old is None:
            description = f"{name} `{change.key}` was added with `{change.new}`"
        elif change.new is None:
            description = f"{name} `{change.key}` was removed"
        else:
            description = (
                f"{name} `{change.key}` changed from `{change.old}` to `{change.new}`"
            )
        lines.append(f"- {description} on {count} manifests.")
    return "\n".join(lines)


def filter_metadata_hunks(
    raw_diff: str, suppress_paths: set[tuple[str, str, str]]
) -> str:
    files = split_diff_files(raw_diff)
    filtered_files = []
    for file_lines in files:
        filtered = filter_file_hunks(file_lines, suppress_paths)
        if filtered:
            filtered_files.extend(filtered)
    return "\n".join(filtered_files).rstrip("\n")


def split_diff_files(raw_diff: str) -> list[list[str]]:
    files: list[list[str]] = []
    current: list[str] = []
    for line in raw_diff.splitlines():
        if line.startswith("diff --git ") and current:
            files.append(current)
            current = []
        current.append(line)
    if current:
        files.append(current)
    return files


def filter_file_hunks(
    file_lines: list[str], suppress_paths: set[tuple[str, str, str]]
) -> list[str]:
    header: list[str] = []
    hunks: list[list[str]] = []
    current_hunk: list[str] | None = None
    for line in file_lines:
        if line.startswith("@@ "):
            if current_hunk is not None:
                hunks.append(current_hunk)
            current_hunk = [line]
        elif current_hunk is None:
            header.append(line)
        else:
            current_hunk.append(line)
    if current_hunk is not None:
        hunks.append(current_hunk)

    kept_hunks = []
    for hunk in hunks:
        if hunk_is_suppressed_metadata(hunk, suppress_paths):
            continue
        filtered_hunk = filter_suppressed_metadata_lines(hunk, suppress_paths)
        if hunk_has_changed_lines(filtered_hunk):
            kept_hunks.append(filtered_hunk)

    if not kept_hunks:
        return []
    return [*header, *[line for hunk in kept_hunks for line in hunk]]


def hunk_is_suppressed_metadata(
    hunk: list[str], suppress_paths: set[tuple[str, str, str]]
) -> bool:
    changed_paths = changed_yaml_paths(hunk)
    if not changed_paths:
        return False
    return all(path in suppress_paths for path in changed_paths)


def changed_yaml_paths(hunk: list[str]) -> set[tuple[str, ...]]:
    old_stack = hunk_header_yaml_stack(hunk[0])
    new_stack = hunk_header_yaml_stack(hunk[0])
    changed_paths: set[tuple[str, ...]] = set()
    for line in hunk[1:]:
        if not line:
            continue
        marker = line[0]
        if marker not in " +-":
            continue
        content = line[1:]
        if marker == " ":
            yaml_mapping_path(content, old_stack)
            yaml_mapping_path(content, new_stack)
            continue
        stack = old_stack if marker == "-" else new_stack
        path = yaml_mapping_path(content, stack)
        if path:
            changed_paths.add(path)
    return changed_paths


def filter_suppressed_metadata_lines(
    hunk: list[str], suppress_paths: set[tuple[str, str, str]]
) -> list[str]:
    changed_paths = changed_yaml_paths(hunk)
    suppressed_keys = {path[-1] for path in suppress_paths}
    suppress_parent_paths = {
        path[:2]
        for path in suppress_paths
        if not any(
            len(changed_path) > 2
            and changed_path[:2] == path[:2]
            and changed_path not in suppress_paths
            for changed_path in changed_paths
        )
    }
    old_stack = hunk_header_yaml_stack(hunk[0])
    new_stack = hunk_header_yaml_stack(hunk[0])
    filtered = [hunk[0]]
    for line in hunk[1:]:
        if not line:
            filtered.append(line)
            continue
        marker = line[0]
        if marker not in " +-":
            filtered.append(line)
            continue
        content = line[1:]
        if marker == " ":
            yaml_mapping_path(content, old_stack)
            yaml_mapping_path(content, new_stack)
            filtered.append(line)
            continue
        stack = old_stack if marker == "-" else new_stack
        path = yaml_mapping_path(content, stack)
        if marker in "+-" and (
            path in suppress_paths
            or path in suppress_parent_paths
            or (
                hunk_header_yaml_stack(hunk[0])
                and yaml_mapping_key(content) in suppressed_keys
            )
        ):
            continue
        filtered.append(line)
    return filtered


def hunk_has_changed_lines(hunk: list[str]) -> bool:
    return any(line.startswith(("+", "-")) for line in hunk[1:])


def hunk_header_yaml_stack(header: str) -> list[tuple[int, str]]:
    match = re.match(r"^@@ .* @@\s+([^:\s][^:]*)\s*:\s*$", header)
    if not match:
        return []
    return [(0, match.group(1).strip().strip("\"'"))]


def yaml_mapping_key(line: str) -> str | None:
    match = re.match(r"^\s*([^:#][^:]*):(?:\s.*)?$", line)
    if not match:
        return None
    return match.group(1).strip().strip("\"'")


def yaml_mapping_path(line: str, stack: list[tuple[int, str]]) -> tuple[str, ...]:
    match = re.match(r"^(\s*)([^:#][^:]*):(?:\s.*)?$", line)
    if not match:
        return tuple(key for _, key in stack)

    indent = len(match.group(1))
    key = match.group(2).strip().strip("\"'")
    while stack and stack[-1][0] >= indent:
        stack.pop()
    path = tuple([key for _, key in stack] + [key])
    stack.append((indent, key))
    return path


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
        sections.append(diff.stat.rstrip())
    if diff.diff.strip():
        sections.append(diff.diff.strip())
    return "\n\n".join(sections) + "\n"


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


def build_comment_body(
    pr_number: str,
    returncode: int,
    diff: ManifestDiff,
    full_diff_reference: str | None = None,
) -> CommentBody:
    del pr_number
    full_diff_reference = full_diff_reference or (
        f"uploaded as Buildkite artifact `{FULL_DIFF_ARTIFACT}`"
    )

    metadata = [f"manifest-builder version: `{MANIFEST_BUILDER_VERSION}`"]

    if returncode:
        metadata.append(f"Exit code: `{returncode}`")

    stat = diff.stat.rstrip()
    context_diff = (
        diff.filtered_diff if diff.filtered_diff is not None else diff.diff
    ).strip()
    summary = diff.summary.strip()
    if not stat and not context_diff and not summary:
        lines = ["The generated output is the same before and after this change"]
        if metadata:
            lines.extend(["", *metadata])
        return CommentBody(body="\n".join(lines), omitted_context_diff=False)

    lines = []
    if stat:
        stat_fence = markdown_fence(stat)
        lines.extend([stat_fence, stat, stat_fence])

    if summary:
        lines.extend(["", "Metadata changes:", "", summary])

    diff_was_filtered = (
        diff.filtered_diff is not None and diff.filtered_diff != diff.diff
    )
    if diff_was_filtered:
        lines.extend(
            [
                "",
                (
                    "_Repeated metadata-only changes have been summarized or omitted. "
                    f"The full diff has been {full_diff_reference}._"
                ),
            ]
        )

    context_lines = []
    if context_diff:
        context_lines = [
            "",
            f"{markdown_fence(context_diff)}diff",
            context_diff,
            markdown_fence(context_diff),
        ]

    body_with_context = "\n".join([*lines, *context_lines, "", *metadata])
    if len(body_with_context) <= MAX_COMMENT_CHARS:
        return CommentBody(
            body=body_with_context, omitted_context_diff=diff_was_filtered
        )

    body_without_context = "\n".join(
        [
            *lines,
            "",
            (
                "_The full context diff is too large for a GitHub comment and "
                f"has been {full_diff_reference}._"
            ),
            "",
            *metadata,
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
