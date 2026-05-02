from __future__ import annotations

import argparse
import logging
import os
import subprocess
import sys
import tomllib
from dataclasses import dataclass
from pathlib import Path
from typing import Any, cast

import yaml

from bktools.image_version_hash import docker_image_tag, git_toplevel

PipelineVariant = str
PipelineOutput = str | None
VALID_VARIANTS = ("rust", "uv", "diffcomment", "rust-container")
VALID_OUTPUTS = ("container",)
PYTHON_PACKAGE_REGISTRY = "nresare/python"

logger = logging.getLogger("pipelinegen")


class PipelineYamlDumper(yaml.SafeDumper):
    def increase_indent(self, flow: bool = False, indentless: bool = False) -> Any:
        return super().increase_indent(flow, False)


def str_presenter(dumper: yaml.SafeDumper, data: str) -> yaml.nodes.ScalarNode:
    style = "|" if "\n" in data else None
    return dumper.represent_scalar("tag:yaml.org,2002:str", data, style=style)


PipelineYamlDumper.add_representer(str, str_presenter)


@dataclass(frozen=True)
class DiffcommentConfig:
    target_repository: str


@dataclass(frozen=True)
class PipelineConfig:
    variant: PipelineVariant
    output: PipelineOutput = None
    diffcomment: DiffcommentConfig | None = None


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

    diffcomment = None
    if variant == "diffcomment":
        diffcomment = read_diffcomment_config(config, config_path)

    return PipelineConfig(variant=variant, output=output, diffcomment=diffcomment)


def read_diffcomment_config(
    config: dict[str, object], config_path: Path
) -> DiffcommentConfig:
    entries = config.get("diffcomment")
    if (
        not isinstance(entries, list)
        or len(entries) != 1
        or not isinstance(entries[0], dict)
    ):
        raise SystemExit(
            f"pipelinegen config {config_path} variant 'diffcomment' requires "
            "exactly one [[diffcomment]] table"
        )

    entry = cast(dict[str, object], entries[0])
    target_repository = entry.get("target_repository")
    if not isinstance(target_repository, str) or not target_repository:
        raise SystemExit(
            f"pipelinegen config {config_path} [[diffcomment]] must contain "
            "string key 'target_repository'"
        )

    return DiffcommentConfig(target_repository=target_repository)


def read_variant(config_path: Path) -> PipelineVariant:
    return read_config(config_path).variant


def render_pipeline_yaml(pipeline: dict[str, object]) -> str:
    return yaml.dump(
        pipeline,
        Dumper=PipelineYamlDumper,
        sort_keys=False,
        default_flow_style=False,
    )


def docker_image_publish_step(tag: str, depends_on: str) -> dict[str, object]:
    image_name, image_tag = tag.split(":", 1)
    return {
        "label": ":whale: build docker image",
        "depends_on": depends_on,
        "agents": {"arch": "arm64"},
        "command": f"docker buildx build -t {tag} .",
        "plugins": [
            {
                "docker-image-push#v1.1.0": {
                    "buildkite": {"auth-method": "oidc"},
                    "image": image_name,
                    "provider": "buildkite",
                    "tag": image_tag,
                }
            }
        ],
    }


def rust_pipeline_yaml(
    tag: str | None = None,
    *,
    output: PipelineOutput = None,
    should_publish: bool = False,
) -> str:
    steps: list[dict[str, object]] = [
        {
            "label": ":rust: rust build and test",
            "env": {"RUSTFLAGS": "-Dwarnings"},
            "commands": [
                "cargo fmt --check",
                "cargo clippy --workspace --locked --all-targets",
                "cargo test --workspace --locked",
            ],
            "key": "test",
        }
    ]

    if output == "container" and should_publish:
        if tag is None:
            raise ValueError("tag is required for container output")
        steps.append(docker_image_publish_step(tag, "test"))

    return render_pipeline_yaml({"steps": steps})


def uv_test_and_build_step(publish: bool = False) -> dict[str, object]:
    commands = [
        "uv run ruff check",
        "uv run ruff format --check",
        "uv run pytest",
        "uv build --wheel",
        "uv run ty check",
    ]
    if publish:
        commands.extend(
            [
                "export UV_PUBLISH_TOKEN=$$(buildkite-agent oidc request-token --audience repo.noa.re)",
                "uv publish --index repo.noa.re",
            ]
        )
    return {
        "label": ":test_tube: Test and Build",
        "key": "test-and-build",
        "command": "\n".join(commands),
    }


def uv_pipeline_yaml(
    tag: str | None = None,
    *,
    output: PipelineOutput = None,
    should_publish: bool = False,
) -> str:
    steps = [uv_test_and_build_step(should_publish and output != "container")]
    if output == "container" and should_publish:
        if tag is None:
            raise ValueError("tag is required for container output")
        steps.append(docker_image_publish_step(tag, "test-and-build"))
    return render_pipeline_yaml({"steps": steps})


def diffcomment_pipeline_yaml(
    tag: str | None = None,
    *,
    output: PipelineOutput = None,
    should_publish: bool = False,
) -> str:
    del tag, output, should_publish
    return render_pipeline_yaml(
        {
            "steps": [
                {
                    "label": ":pipeline:",
                    "command": "\n".join(
                        [
                            "uv venv",
                            "uv pip install --pre --upgrade bktools \\",
                            '  --extra-index-url="https://repo.noa.re"',
                            "uv run diffcomment",
                        ]
                    ),
                }
            ]
        }
    )


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
