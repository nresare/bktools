from __future__ import annotations

import argparse
import logging
import os
import re
import shlex
import subprocess
import sys
import tomllib
from dataclasses import dataclass
from pathlib import Path
from typing import Any, cast

import yaml

from bktools.image_version_hash import docker_image_tag, git_toplevel, package_name

PipelineVariant = str
PipelineOutput = str | None
VALID_VARIANTS = ("rust", "uv", "manifest-builder", "relcoord", "rust-container")
VALID_OUTPUTS = ("container",)
PYTHON_PACKAGE_REGISTRY = "nresare/python"
DEFAULT_AGENTS = {"speed": "fast"}
BUILD_ARCHES = ("amd64", "arm64")
BUILDX_METADATA_FILE = "buildx-metadata.json"
CONTAINER_REGISTRY = "repo.noa.re"
IDCAT_ENDPOINT = "https://idcat.noa.re"
GITHUB_APP = "nresare-buildsystem"
NOTIFY_RELCOORD_COMMAND = (
    "uvx --from bktools --index https://repo.noa.re notify-relcoord"
)

logger = logging.getLogger("pipelinegen")


class PipelineYamlDumper(yaml.SafeDumper):
    def increase_indent(self, flow: bool = False, indentless: bool = False) -> Any:
        return super().increase_indent(flow, False)


def str_presenter(dumper: yaml.SafeDumper, data: str) -> yaml.nodes.ScalarNode:
    style = "|" if "\n" in data else None
    return dumper.represent_scalar("tag:yaml.org,2002:str", data, style=style)


PipelineYamlDumper.add_representer(str, str_presenter)


@dataclass(frozen=True)
class ManifestBuilderConfig:
    repo: str


@dataclass(frozen=True)
class PipelineConfig:
    variant: PipelineVariant
    output: PipelineOutput = None
    manifest_builder: ManifestBuilderConfig | None = None
    relcoord_endpoint: str | None = None


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

    relcoord_endpoint = read_relcoord_endpoint(config, config_path)

    if variant == "rust-container":
        logger.warning(
            "pipelinegen config variant 'rust-container' is deprecated; "
            "use variant = 'rust' and output = 'container' instead"
        )
        return PipelineConfig(
            variant="rust",
            output="container",
            relcoord_endpoint=relcoord_endpoint,
        )

    manifest_builder = None
    if variant == "manifest-builder":
        manifest_builder = read_manifest_builder_config(config, config_path)
    if variant == "relcoord" and relcoord_endpoint is None:
        raise SystemExit(
            f"pipelinegen config {config_path} variant 'relcoord' "
            "requires string key 'relcoord-endpoint'"
        )
    if variant == "relcoord" and output is not None:
        raise SystemExit(
            f"pipelinegen config {config_path} variant 'relcoord' "
            "does not support 'output'"
        )

    return PipelineConfig(
        variant=variant,
        output=output,
        manifest_builder=manifest_builder,
        relcoord_endpoint=relcoord_endpoint,
    )


def read_manifest_builder_config(
    config: dict[str, object], config_path: Path
) -> ManifestBuilderConfig:
    repo = config.get("repo")
    if not isinstance(repo, str) or not repo:
        raise SystemExit(
            f"pipelinegen config {config_path} variant 'manifest-builder' "
            "requires string key 'repo'"
        )

    return ManifestBuilderConfig(repo=repo)


def read_relcoord_endpoint(config: dict[str, object], config_path: Path) -> str | None:
    endpoint = config.get("relcoord-endpoint")
    if endpoint is None:
        return None
    if not isinstance(endpoint, str) or not endpoint:
        raise SystemExit(
            f"pipelinegen config {config_path} key 'relcoord-endpoint' "
            "must be a non-empty string"
        )
    return endpoint


def read_variant(config_path: Path) -> PipelineVariant:
    return read_config(config_path).variant


def render_pipeline_yaml(pipeline: dict[str, object]) -> str:
    pipeline = apply_default_agents(pipeline)
    rendered = yaml.dump(
        pipeline,
        Dumper=PipelineYamlDumper,
        sort_keys=False,
        default_flow_style=False,
    )
    return wrap_docker_buildx_command(rendered)


def wrap_docker_buildx_command(rendered: str) -> str:
    return re.sub(
        r"^(?P<indent>\s*)- (?P<command>docker buildx build(?: \.)?) "
        r"--output (?P<output>.+)$",
        r"\g<indent>- \g<command>\n\g<indent>  --output \g<output>",
        rendered,
        flags=re.MULTILINE,
    )


def apply_default_agents(pipeline: dict[str, object]) -> dict[str, object]:
    steps = pipeline.get("steps")
    if not isinstance(steps, list):
        return pipeline

    normalized_steps = []
    for step in steps:
        if not isinstance(step, dict):
            normalized_steps.append(step)
            continue

        step_dict = cast(dict[str, object], step)
        agents = step_dict.get("agents")
        agent_tags = cast(dict[str, object], agents) if isinstance(agents, dict) else {}
        if agent_tags.get("arch") == "arm64":
            normalized_steps.append(step_dict)
            continue

        normalized_step = step_dict.copy()
        normalized_step["agents"] = {
            **agent_tags,
            **DEFAULT_AGENTS,
        }
        normalized_steps.append(normalized_step)

    return {**pipeline, "steps": normalized_steps}


def registry_login_commands() -> list[str]:
    return [
        f"token=$$(buildkite-agent oidc request-token --audience {CONTAINER_REGISTRY})",
        f"echo $$token | docker login --password-stdin -u token {CONTAINER_REGISTRY}",
    ]


def docker_image_build_step(
    image_repo: str,
    depends_on: str,
    *,
    arch: str,
) -> dict[str, object]:
    return {
        "label": f":whale: build docker image ({arch})",
        "key": build_step_key(arch),
        "depends_on": depends_on,
        "agents": {"arch": arch},
        "commands": [
            *registry_login_commands(),
            (
                f"docker buildx build --progress=plain . --platform linux/{arch} "
                f"--metadata-file {BUILDX_METADATA_FILE} "
                f"--output type=image,name={image_repo},push-by-digest=true,"
                "name-canonical=true,push=true,compression=zstd"
            ),
            (
                f"buildkite-agent meta-data set {digest_metadata_key(arch)} "
                f'"$$(jq -r \'."containerimage.digest"\' {BUILDX_METADATA_FILE})"'
            ),
        ],
    }


def docker_manifest_push_step(
    image_repo: str,
    tag: str,
    *,
    relcoord_endpoint: str | None = None,
) -> dict[str, object]:
    image = container_image(image_repo, tag)
    sources = " ".join(
        f"{image_repo}@$$(buildkite-agent meta-data get {digest_metadata_key(arch)})"
        for arch in BUILD_ARCHES
    )
    commands = [
        *registry_login_commands(),
        f"docker buildx imagetools create -t {image} {sources}",
    ]
    if relcoord_endpoint is not None:
        commands.extend(
            [
                (
                    f"{NOTIFY_RELCOORD_COMMAND} "
                    f"{shlex.quote(relcoord_endpoint)} "
                    f"--repo {shlex.quote(image_repo)} "
                    f"--tag {shlex.quote(tag)}"
                ),
            ]
        )

    return {
        "label": ":whale: push multi-arch docker image",
        "key": "docker-manifest",
        "depends_on": [build_step_key(arch) for arch in BUILD_ARCHES],
        "commands": commands,
    }


def docker_image_publish_steps(
    image_repo: str,
    tag: str,
    depends_on: str,
    *,
    relcoord_endpoint: str | None = None,
) -> list[dict[str, object]]:
    steps: list[dict[str, object]] = [
        docker_image_build_step(image_repo, depends_on, arch=arch)
        for arch in BUILD_ARCHES
    ]
    steps.append(
        docker_manifest_push_step(image_repo, tag, relcoord_endpoint=relcoord_endpoint)
    )
    return steps


def build_step_key(arch: str) -> str:
    return f"docker-build-{arch}"


def digest_metadata_key(arch: str) -> str:
    return f"digest-{arch}"


def container_image(image_repo: str, tag: str) -> str:
    return f"{image_repo}:{tag}"


def container_image_repo(repo_suffix: str) -> str:
    return f"{CONTAINER_REGISTRY}/{repo_suffix}"


def rust_pipeline_yaml(
    tag: str | None = None,
    *,
    image_repo: str | None = None,
    output: PipelineOutput = None,
    should_publish: bool = False,
    relcoord_endpoint: str | None = None,
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
        if image_repo is None:
            raise ValueError("image_repo is required for container output")
        steps.extend(
            docker_image_publish_steps(
                image_repo, tag, "test", relcoord_endpoint=relcoord_endpoint
            )
        )

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
    image_repo: str | None = None,
    output: PipelineOutput = None,
    should_publish: bool = False,
    relcoord_endpoint: str | None = None,
) -> str:
    steps = [uv_test_and_build_step(should_publish and output != "container")]
    if output == "container" and should_publish:
        if tag is None:
            raise ValueError("tag is required for container output")
        if image_repo is None:
            raise ValueError("image_repo is required for container output")
        steps.extend(
            docker_image_publish_steps(
                image_repo, tag, "test-and-build", relcoord_endpoint=relcoord_endpoint
            )
        )
    return render_pipeline_yaml({"steps": steps})


def diffcomment_pipeline_yaml(
    target_repository: str,
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
                    "label": ":pipeline: Add comment to PR with diff",
                    "command": "\n".join(
                        [
                            "uv venv",
                            "uv pip install --upgrade bktools \\",
                            '  --extra-index-url="https://repo.noa.re"',
                            (
                                "pr_comment_token=$$(uv run get-installation-token "
                                f"--endpoint {IDCAT_ENDPOINT} "
                                f"--github-app {GITHUB_APP} "
                                '--repo "$$BUILDKITE_REPO")'
                            ),
                            (
                                "uv run diffcomment --target-repo "
                                f"{shlex.quote(target_repository)} "
                                '--pr-comment-token "$$pr_comment_token"'
                            ),
                        ]
                    ),
                }
            ]
        }
    )


def manifest_builder_pipeline_yaml(
    repo: str,
    tag: str | None = None,
    *,
    output: PipelineOutput = None,
    should_publish: bool = False,
    is_pull_request: bool = False,
) -> str:
    del tag, output
    if is_pull_request:
        return diffcomment_pipeline_yaml(repo)

    if not should_publish:
        logger.info(
            "manifest-builder invocation was not from a pr or on the main branch, "
            "so no additional pipeline steps are needed"
        )
        return EMPTY_PIPELINE_YAML

    return render_pipeline_yaml(
        {
            "steps": [
                {
                    "label": ":pipeline: Generate and push manifests",
                    "command": "\n".join(
                        [
                            "uv venv",
                            "uv pip install --upgrade bktools \\",
                            '  --extra-index-url="https://repo.noa.re"',
                            (
                                "uv run manifest-builder-on-checkout --repo "
                                f"{shlex.quote(repo)}"
                            ),
                        ]
                    ),
                }
            ]
        }
    )


def relcoord_pipeline_yaml(
    endpoint: str,
    *,
    should_publish: bool = False,
    is_pull_request: bool = False,
) -> str:
    if not is_pull_request and not should_publish:
        return EMPTY_PIPELINE_YAML

    mode = "diffcomment" if is_pull_request else "change"
    return render_pipeline_yaml(
        {
            "steps": [
                {
                    "label": ":pipeline: Request relcoord system " + mode,
                    "command": (
                        f"{NOTIFY_RELCOORD_COMMAND} {shlex.quote(endpoint)} --system"
                        + (" --diffcomment" if is_pull_request else "")
                    ),
                }
            ]
        }
    )


def pipeline_yaml(
    tag: str | None = None,
    *,
    image_repo: str | None = None,
    variant: PipelineVariant = "rust",
    output: PipelineOutput = None,
    should_publish: bool = False,
    manifest_builder: ManifestBuilderConfig | None = None,
    relcoord_endpoint: str | None = None,
    is_pull_request: bool = False,
) -> str:
    if variant == "rust-container":
        variant = "rust"
        output = "container"

    if variant == "rust":
        return rust_pipeline_yaml(
            tag,
            image_repo=image_repo,
            output=output,
            should_publish=should_publish,
            relcoord_endpoint=relcoord_endpoint,
        )

    if variant == "uv":
        return uv_pipeline_yaml(
            tag,
            image_repo=image_repo,
            output=output,
            should_publish=should_publish,
            relcoord_endpoint=relcoord_endpoint,
        )

    if variant == "manifest-builder":
        if manifest_builder is None:
            raise ValueError(
                "manifest-builder config is required for manifest-builder variant"
            )
        return manifest_builder_pipeline_yaml(
            manifest_builder.repo,
            tag,
            output=output,
            should_publish=should_publish,
            is_pull_request=is_pull_request,
        )

    if variant == "relcoord":
        if relcoord_endpoint is None:
            raise ValueError("relcoord-endpoint is required for relcoord variant")
        return relcoord_pipeline_yaml(
            relcoord_endpoint,
            should_publish=should_publish,
            is_pull_request=is_pull_request,
        )

    raise ValueError(f"unknown pipeline variant: {variant}")


def is_pull_request_build() -> bool:
    return os.getenv("BUILDKITE_PULL_REQUEST") not in (None, "", "false")


PIPELINE_ARTIFACT = "pipeline.yaml"
EMPTY_PIPELINE_YAML = render_pipeline_yaml({"steps": []})


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
    image_repo = None
    upload_target = PYTHON_PACKAGE_REGISTRY
    if config.output == "container":
        tag = docker_image_tag(repo_root)
        repo_suffix = package_name(repo_root)
        image_repo = container_image_repo(repo_suffix)
        upload_target = repo_suffix

    branch = os.getenv("BUILDKITE_BRANCH", "")
    if config.variant not in ("manifest-builder", "relcoord"):
        if should_publish:
            logger.info("building on main branch, uploading to %s", upload_target)
        elif branch:
            logger.info("building on %s branch, not uploading", branch)
        else:
            logger.info("not building on main branch, not uploading")

    yaml = pipeline_yaml(
        tag,
        image_repo=image_repo,
        variant=config.variant,
        output=config.output,
        should_publish=should_publish,
        manifest_builder=config.manifest_builder,
        relcoord_endpoint=config.relcoord_endpoint,
        is_pull_request=is_pull_request_build(),
    )
    if args.dump:
        sys.stdout.write(yaml)
        return

    if yaml == EMPTY_PIPELINE_YAML:
        return

    write_pipeline_artifact(repo_root, yaml)
    upload_pipeline_artifact(repo_root)
    upload_pipeline(yaml)


if __name__ == "__main__":
    main()
