from __future__ import annotations

import argparse
import os
import sys
from pathlib import Path

from bktools.image_version_hash import docker_image_tag, git_toplevel

PipelineVariant = str


def rust_container_pipeline_yaml(tag: str, should_publish: bool = False) -> str:
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

    if should_publish:
        image_name, image_tag = tag.split(":", 1)
        lines.extend(
            [
                "  - label: ':whale: build docker image'",
                "    depends_on: test",
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
        )

    return "\n".join(lines) + "\n"


def uv_pipeline_yaml(should_publish: bool = False) -> str:
    lines = [
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

    if should_publish:
        lines.extend(
            [
                "  - label: Publish",
                "    depends_on: test-and-build",
                "    plugins:",
                "      - publish-to-packages#v2.2.0:",
                '          artifacts: "dist/*.whl"',
                '          registry: "nresare/python"',
            ]
        )

    return "\n".join(lines) + "\n"


def pipeline_yaml(
    tag: str | None = None,
    *,
    variant: PipelineVariant = "rust-container",
    should_publish: bool = False,
) -> str:
    if variant == "rust-container":
        if tag is None:
            raise ValueError("tag is required for the rust-container pipeline variant")
        return rust_container_pipeline_yaml(tag, should_publish)

    if variant == "uv":
        return uv_pipeline_yaml(should_publish)

    raise ValueError(f"unknown pipeline variant: {variant}")


def main() -> None:
    parser = argparse.ArgumentParser(description="Emit a Buildkite pipeline.")
    parser.add_argument(
        "--variant",
        choices=("rust-container", "uv"),
        default="rust-container",
        help="Pipeline variant to emit.",
    )
    parser.add_argument(
        "--repo-root",
        type=Path,
        default=None,
        help="Repository root to hash. Defaults to the current git work tree.",
    )
    args = parser.parse_args()

    should_publish = os.getenv("BUILDKITE_BRANCH") == "main"
    tag = None
    if args.variant == "rust-container":
        repo_root = (
            args.repo_root if args.repo_root is not None else git_toplevel(Path.cwd())
        )
        tag = docker_image_tag(repo_root)

    sys.stdout.write(
        pipeline_yaml(tag, variant=args.variant, should_publish=should_publish)
    )


if __name__ == "__main__":
    main()
