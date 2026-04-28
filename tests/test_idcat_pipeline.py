import re
import subprocess
from pathlib import Path

import pytest

from bktools.idcat_pipeline import (
    PipelineConfig,
    main,
    pipeline_yaml,
    read_config,
    read_variant,
    upload_pipeline_artifact,
    upload_pipeline,
    uv_pipeline_yaml,
    write_pipeline_artifact,
)


def test_pipeline_yaml_without_publish_contains_test_step_only() -> None:
    pipeline = pipeline_yaml("idcat:0.1.0-deadbeef", variant="rust-container")

    assert "key: test" in pipeline
    assert "docker buildx build" not in pipeline


def test_pipeline_yaml_with_publish_adds_docker_push_step() -> None:
    pipeline = pipeline_yaml(
        "idcat:0.1.0-deadbeef", variant="rust-container", should_publish=True
    )

    assert "depends_on: test" in pipeline
    assert "command: docker buildx build -t idcat:0.1.0-deadbeef ." in pipeline
    assert "image: idcat" in pipeline
    assert "tag: 0.1.0-deadbeef" in pipeline


def test_rust_pipeline_with_container_output_adds_docker_push_step() -> None:
    pipeline = pipeline_yaml(
        "idcat:0.1.0-deadbeef",
        variant="rust",
        output="container",
        should_publish=True,
    )

    assert "depends_on: test" in pipeline
    assert "command: docker buildx build -t idcat:0.1.0-deadbeef ." in pipeline
    assert "image: idcat" in pipeline
    assert "tag: 0.1.0-deadbeef" in pipeline


def test_uv_pipeline_yaml_without_publish_contains_test_and_build_step_only() -> None:
    pipeline = uv_pipeline_yaml()

    assert 'label: ":test_tube: Test and Build"' in pipeline
    assert "uv build --wheel" in pipeline
    assert "publish-to-packages" not in pipeline
    assert "branches: main" not in pipeline


def test_uv_pipeline_yaml_with_publish_adds_publish_step() -> None:
    pipeline = uv_pipeline_yaml(should_publish=True)

    assert "depends_on: test-and-build" in pipeline
    assert "publish-to-packages#v2.2.0" in pipeline
    assert 'artifacts: "dist/*.whl"' in pipeline
    assert 'registry: "nresare/python"' in pipeline
    assert "branches: main" not in pipeline


def test_uv_pipeline_with_container_output_uses_uv_steps_and_docker_publish() -> None:
    pipeline = pipeline_yaml(
        "idcat:0.1.0-deadbeef",
        variant="uv",
        output="container",
        should_publish=True,
    )

    assert "uv run ruff check" in pipeline
    assert "uv run pytest" in pipeline
    assert "uv build --wheel" in pipeline
    assert "uv run ty check" in pipeline
    assert "depends_on: test-and-build" in pipeline
    assert "command: docker buildx build -t idcat:0.1.0-deadbeef ." in pipeline
    assert "docker-image-push#v1.1.0" in pipeline
    assert "image: idcat" in pipeline
    assert "tag: 0.1.0-deadbeef" in pipeline
    assert "publish-to-packages" not in pipeline


def test_uv_pipeline_with_container_output_without_publish_has_no_publish_step() -> (
    None
):
    pipeline = pipeline_yaml("idcat:0.1.0-deadbeef", variant="uv", output="container")

    assert "uv run pytest" in pipeline
    assert "docker-image-push" not in pipeline
    assert "publish-to-packages" not in pipeline


def test_pipeline_yaml_dispatches_to_uv_variant_without_tag() -> None:
    pipeline = pipeline_yaml(variant="uv")

    assert "uv run pytest" in pipeline


def test_pipeline_yaml_requires_tag_for_container_output() -> None:
    with pytest.raises(ValueError, match="container output"):
        pipeline_yaml(variant="uv", output="container", should_publish=True)


def test_read_config_loads_variant_and_output_from_config(tmp_path: Path) -> None:
    config_path = tmp_path / "pipelinegen.toml"
    config_path.write_text('variant = "uv"\noutput = "container"\n')

    assert read_config(config_path) == PipelineConfig(variant="uv", output="container")


def test_read_variant_loads_variant_from_config(tmp_path: Path) -> None:
    config_path = tmp_path / "pipelinegen.toml"
    config_path.write_text('variant = "uv"\n')

    assert read_variant(config_path) == "uv"


def test_read_config_warns_and_maps_deprecated_rust_container(
    tmp_path: Path, caplog: pytest.LogCaptureFixture
) -> None:
    config_path = tmp_path / "pipelinegen.toml"
    config_path.write_text('variant = "rust-container"\n')

    config = read_config(config_path)

    assert config == PipelineConfig(variant="rust", output="container")
    assert "variant 'rust-container' is deprecated" in caplog.text


def test_read_variant_rejects_unknown_variant(tmp_path: Path) -> None:
    config_path = tmp_path / "pipelinegen.toml"
    config_path.write_text('variant = "ruby"\n')

    with pytest.raises(SystemExit):
        read_variant(config_path)


def test_main_uses_config_variant_and_logs_publish_target(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    config_dir = tmp_path / ".buildkite"
    config_dir.mkdir()
    (config_dir / "pipelinegen.toml").write_text('variant = "uv"\n')
    monkeypatch.setattr(
        "sys.argv", ["pipelinegen", "--dump", "--repo-root", str(tmp_path)]
    )
    monkeypatch.setenv("BUILDKITE_BRANCH", "main")

    main()

    captured = capsys.readouterr()
    assert "uv run pytest" in captured.out
    assert re.search(
        rf"\d{{4}}-\d{{2}}-\d{{2}} \d{{2}}:\d{{2}}:\d{{2}} INFO reading config from {re.escape(str(config_dir / 'pipelinegen.toml'))}",
        captured.err,
    )
    assert "building on main branch, uploading to nresare/python" in captured.err


def test_main_uses_uv_container_output_and_logs_docker_target(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    config_dir = tmp_path / ".buildkite"
    config_dir.mkdir()
    (config_dir / "pipelinegen.toml").write_text(
        'variant = "uv"\noutput = "container"\n'
    )
    monkeypatch.setattr(
        "sys.argv", ["pipelinegen", "--dump", "--repo-root", str(tmp_path)]
    )
    monkeypatch.setenv("BUILDKITE_BRANCH", "main")
    monkeypatch.setattr(
        "bktools.idcat_pipeline.docker_image_tag",
        lambda repo_root: "idcat:0.1.0-deadbeef",
    )

    main()

    captured = capsys.readouterr()
    assert "uv run pytest" in captured.out
    assert "command: docker buildx build -t idcat:0.1.0-deadbeef ." in captured.out
    assert "building on main branch, uploading to idcat" in captured.err


def test_upload_pipeline_invokes_buildkite_agent(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    calls = []

    def fake_run(
        args: list[str], *, input: str, text: bool, check: bool
    ) -> subprocess.CompletedProcess[str]:
        calls.append((args, input, text, check))
        return subprocess.CompletedProcess(args, 0)

    monkeypatch.setattr("subprocess.run", fake_run)

    upload_pipeline("steps: []\n")

    assert calls == [
        (["buildkite-agent", "pipeline", "upload"], "steps: []\n", True, True)
    ]


def test_write_pipeline_artifact_writes_pipeline_yaml(tmp_path: Path) -> None:
    artifact_path = write_pipeline_artifact(tmp_path, "steps: []\n")

    assert artifact_path == tmp_path / "pipeline.yaml"
    assert artifact_path.read_text() == "steps: []\n"


def test_upload_pipeline_artifact_invokes_buildkite_agent(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    calls = []

    def fake_run(
        args: list[str], *, cwd: Path, check: bool
    ) -> subprocess.CompletedProcess[str]:
        calls.append((args, cwd, check))
        return subprocess.CompletedProcess(args, 0)

    monkeypatch.setattr("subprocess.run", fake_run)

    upload_pipeline_artifact(tmp_path)

    assert calls == [
        (["buildkite-agent", "artifact", "upload", "pipeline.yaml"], tmp_path, True)
    ]


def test_main_uploads_by_default(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    config_dir = tmp_path / ".buildkite"
    config_dir.mkdir()
    (config_dir / "pipelinegen.toml").write_text('variant = "uv"\n')
    uploaded = []
    uploaded_artifacts = []
    monkeypatch.setattr("sys.argv", ["pipelinegen", "--repo-root", str(tmp_path)])
    monkeypatch.setattr("bktools.idcat_pipeline.upload_pipeline", uploaded.append)
    monkeypatch.setattr(
        "bktools.idcat_pipeline.upload_pipeline_artifact", uploaded_artifacts.append
    )

    main()

    captured = capsys.readouterr()
    assert captured.out == ""
    assert len(uploaded) == 1
    assert "uv run pytest" in uploaded[0]
    assert uploaded_artifacts == [tmp_path]
    assert (tmp_path / "pipeline.yaml").read_text() == uploaded[0]


def test_dump_does_not_write_pipeline_artifact(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    config_dir = tmp_path / ".buildkite"
    config_dir.mkdir()
    (config_dir / "pipelinegen.toml").write_text('variant = "uv"\n')
    monkeypatch.setattr(
        "sys.argv", ["pipelinegen", "--dump", "--repo-root", str(tmp_path)]
    )

    main()

    assert not (tmp_path / "pipeline.yaml").exists()
