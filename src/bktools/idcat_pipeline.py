from __future__ import annotations

import argparse
import logging
import os
import subprocess
import sys
import textwrap
import tomllib
from dataclasses import dataclass
from pathlib import Path

from bktools.image_version_hash import docker_image_tag, git_toplevel

PipelineVariant = str
PipelineOutput = str | None
VALID_VARIANTS = ("rust", "uv", "diffcomment", "rust-container")
VALID_OUTPUTS = ("container",)
PYTHON_PACKAGE_REGISTRY = "nresare/python"

logger = logging.getLogger("pipelinegen")


@dataclass(frozen=True)
class PipelineConfig:
    variant: PipelineVariant
    output: PipelineOutput = None


def read_config(config_path: Path) -> PipelineConfig:
    logger.info("reading config from %s", config_path)
    try:
        config = tomllib.loads(config_path.read_text())
    except FileNotFoundError as error:
        raise SystemExit(f"pipelinegen config not found: {config_path}") from error
    except tomllib.TOMLDecodeError as error:
        raise SystemExit(
            f"failed to parse pipelinegen config {config_path}: {error}"
        ) from error

    variant = config.get("variant")
    if not isinstance(variant, str):
        raise SystemExit(
            f"pipelinegen config {config_path} must contain string key 'variant'"
        )
    if variant not in VALID_VARIANTS:
        valid_variants = ", ".join(VALID_VARIANTS)
        raise SystemExit(
            f"pipelinegen config {config_path} has unsupported variant {variant!r}; "
            f"expected one of: {valid_variants}"
        )

    output = config.get("output")
    if output is not None and not isinstance(output, str):
        raise SystemExit(
            f"pipelinegen config {config_path} key 'output' must be a string"
        )
    if output is not None and output not in VALID_OUTPUTS:
        valid_outputs = ", ".join(VALID_OUTPUTS)
        raise SystemExit(
            f"pipelinegen config {config_path} has unsupported output {output!r}; "
            f"expected one of: {valid_outputs}"
        )

    if variant == "rust-container":
        logger.warning(
            "pipelinegen config variant 'rust-container' is deprecated; "
            "use variant = 'rust' and output = 'container' instead"
        )
        return PipelineConfig(variant="rust", output="container")

    return PipelineConfig(variant=variant, output=output)


def read_variant(config_path: Path) -> PipelineVariant:
    return read_config(config_path).variant


def docker_image_publish_step(tag: str, depends_on: str) -> list[str]:
    image_name, image_tag = tag.split(":", 1)
    return [
        "  - label: ':whale: build docker image'",
        f"    depends_on: {depends_on}",
        "    agents:",
        "      arch: arm64",
        f"    command: docker buildx build -t {tag} .",
        "    plugins:",
        "      - docker-image-push#v1.1.0:",
        "          buildkite:",
        "            auth-method: oidc",
        f"          image: {image_name}",
        "          provider: buildkite",
        f"          tag: {image_tag}",
    ]


def rust_pipeline_yaml(
    tag: str | None = None,
    *,
    output: PipelineOutput = None,
    should_publish: bool = False,
) -> str:
    lines = [
        "steps:",
        "  - label: ':rust: rust build and test'",
        "    env:",
        "      RUSTFLAGS: -Dwarnings",
        "    commands:",
        "      - cargo fmt --check",
        "      - cargo clippy --workspace --locked --all-targets",
        "      - cargo test --workspace --locked",
        "    key: test",
    ]

    if output == "container" and should_publish:
        if tag is None:
            raise ValueError("tag is required for container output")
        lines.extend(docker_image_publish_step(tag, "test"))

    return "\n".join(lines) + "\n"


def uv_test_and_build_step() -> list[str]:
    return [
        "steps:",
        '  - label: ":test_tube: Test and Build"',
        "    key: test-and-build",
        "    command: |",
        "      uv run ruff check",
        "      uv run ruff format --check",
        "      uv run pytest",
        "      # the wheel build needs to happen before the ty check on havtorn for some reason, to generate _version",
        "      uv build --wheel",
        "      uv run ty check",
        "    artifact_paths:",
        '      - "dist/*.whl"',
    ]


def uv_pipeline_yaml(
    tag: str | None = None,
    *,
    output: PipelineOutput = None,
    should_publish: bool = False,
) -> str:
    lines = uv_test_and_build_step()

    if output == "container" and should_publish:
        if tag is None:
            raise ValueError("tag is required for container output")
        lines.extend(docker_image_publish_step(tag, "test-and-build"))
    elif should_publish:
        lines.extend(
            [
                "  - label: Publish",
                "    depends_on: test-and-build",
                "    plugins:",
                "      - publish-to-packages#v2.2.0:",
                '          artifacts: "dist/*.whl"',
                f'          registry: "{PYTHON_PACKAGE_REGISTRY}"',
            ]
        )

    return "\n".join(lines) + "\n"


DIFFCOMMENT_SCRIPT = r"""from __future__ import annotations

import json
import os
import re
import subprocess
import sys
from contextlib import redirect_stderr, redirect_stdout
from io import StringIO
import urllib.error
import urllib.request

from manifest_builder.cli import main as manifest_builder_main


AUDIENCE = "idcat.noa.re"
IDCAT_BASE_URL = "https://idcat.noa.re/proxy"
GITHUB_API_VERSION = "2026-03-10"
MAX_COMMENT_BYTES = 60_000


def main() -> int:
    pr_number = os.environ.get("BUILDKITE_PULL_REQUEST")
    if not pr_number or pr_number == "false":
        print("Skipping manifest diff comment because this build was not triggered by a pull request.")
        return 0

    returncode, output = run_manifest_builder_diff()

    owner, repo = github_repo()
    token = request_idcat_token()
    body = build_comment_body(pr_number, returncode, output)
    post_issue_comment(token, owner, repo, pr_number, body)

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
    repo_url = os.environ.get("BUILDKITE_PULL_REQUEST_REPO") or os.environ.get("BUILDKITE_REPO", "")
    match = re.search(r"github\.com[:/]([^/]+)/([^/]+?)(?:\.git)?$", repo_url)
    if not match:
        raise RuntimeError(f"could not infer GitHub owner/repo from Buildkite repo URL: {repo_url!r}")
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
    longest_backtick_run = max((len(match.group(0)) for match in re.finditer(r"`+", text)), default=0)
    return "`" * max(3, longest_backtick_run + 1)


def truncate_comment(body: str) -> str:
    encoded = body.encode()
    if len(encoded) <= MAX_COMMENT_BYTES:
        return body

    suffix = "\n\n_Output truncated to fit within the GitHub comment size limit._"
    allowed_bytes = MAX_COMMENT_BYTES - len(suffix.encode())
    truncated = encoded[:allowed_bytes].decode(errors="ignore").rstrip()
    return f"{truncated}{suffix}"


def post_issue_comment(token: str, owner: str, repo: str, pr_number: str, body: str) -> None:
    url = f"{IDCAT_BASE_URL}/nresare-buildsystem/repos/{owner}/{repo}/issues/{pr_number}/comments"
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
        print(error.read().decode(errors="replace"), file=sys.stderr)
        raise


if __name__ == "__main__":
    raise SystemExit(main())
"""


def diffcomment_pipeline_yaml(
    tag: str | None = None,
    *,
    output: PipelineOutput = None,
    should_publish: bool = False,
) -> str:
    del tag, output, should_publish
    script = textwrap.indent(DIFFCOMMENT_SCRIPT, "      ")
    lines = [
        "steps:",
        "  - label: ':memo: manifest diff comment'",
        "    command: |",
        "      python - <<'PY'",
        script,
        "      PY",
    ]
    return "\n".join(lines) + "\n"


def pipeline_yaml(
    tag: str | None = None,
    *,
    variant: PipelineVariant = "rust",
    output: PipelineOutput = None,
    should_publish: bool = False,
) -> str:
    if variant == "rust-container":
        variant = "rust"
        output = "container"

    if variant == "rust":
        return rust_pipeline_yaml(tag, output=output, should_publish=should_publish)

    if variant == "uv":
        return uv_pipeline_yaml(tag, output=output, should_publish=should_publish)

    if variant == "diffcomment":
        return diffcomment_pipeline_yaml(
            tag, output=output, should_publish=should_publish
        )

    raise ValueError(f"unknown pipeline variant: {variant}")


PIPELINE_ARTIFACT = "pipeline.yaml"


def write_pipeline_artifact(repo_root: Path, yaml: str) -> Path:
    artifact_path = repo_root / PIPELINE_ARTIFACT
    logger.info("writing generated pipeline to %s", artifact_path)
    artifact_path.write_text(yaml)
    return artifact_path


def upload_pipeline_artifact(repo_root: Path) -> None:
    logger.info("uploading pipeline artifact %s", PIPELINE_ARTIFACT)
    subprocess.run(
        ["buildkite-agent", "artifact", "upload", PIPELINE_ARTIFACT],
        cwd=repo_root,
        check=True,
    )


def upload_pipeline(yaml: str) -> None:
    logger.info("uploading pipeline with buildkite-agent pipeline upload")
    subprocess.run(
        ["buildkite-agent", "pipeline", "upload"],
        input=yaml,
        text=True,
        check=True,
    )


def main() -> None:
    parser = argparse.ArgumentParser(description="Emit a Buildkite pipeline.")
    parser.add_argument(
        "--dump",
        action="store_true",
        help="Write generated pipeline YAML to stdout instead of uploading it with buildkite-agent.",
    )
    parser.add_argument(
        "--repo-root",
        type=Path,
        default=None,
        help="Repository root containing .buildkite/pipelinegen.toml. Defaults to the current git work tree.",
    )
    args = parser.parse_args()

    logging.basicConfig(
        format="%(asctime)s %(levelname)s %(message)s",
        datefmt="%Y-%m-%d %H:%M:%S",
        level=logging.INFO,
        stream=sys.stderr,
        force=True,
    )
    repo_root = (
        args.repo_root if args.repo_root is not None else git_toplevel(Path.cwd())
    )
    config = read_config(repo_root / ".buildkite" / "pipelinegen.toml")
    should_publish = os.getenv("BUILDKITE_BRANCH") == "main"
    tag = None
    upload_target = PYTHON_PACKAGE_REGISTRY
    if config.output == "container":
        tag = docker_image_tag(repo_root)
        upload_target = tag.split(":", 1)[0]

    branch = os.getenv("BUILDKITE_BRANCH", "")
    if should_publish:
        logger.info("building on main branch, uploading to %s", upload_target)
    elif branch:
        logger.info("building on %s branch, not uploading", branch)
    else:
        logger.info("not building on main branch, not uploading")

    yaml = pipeline_yaml(
        tag,
        variant=config.variant,
        output=config.output,
        should_publish=should_publish,
    )
    if args.dump:
        sys.stdout.write(yaml)
        return

    write_pipeline_artifact(repo_root, yaml)
    upload_pipeline_artifact(repo_root)
    upload_pipeline(yaml)


if __name__ == "__main__":
    main()
