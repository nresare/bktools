import re
import subprocess
from pathlib import Path

import pytest

from bktools.idcat_pipeline import (
    main,
    pipeline_yaml,
    read_variant,
    upload_pipeline,
    uv_pipeline_yaml,
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


def test_pipeline_yaml_dispatches_to_uv_variant_without_tag() -> None:
    pipeline = pipeline_yaml(variant="uv")

    assert "uv run pytest" in pipeline


def test_read_variant_loads_variant_from_config(tmp_path: Path) -> None:
    config_path = tmp_path / "pipelinegen.toml"
    config_path.write_text('variant = "uv"\n')

    assert read_variant(config_path) == "uv"


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


def test_main_uploads_by_default(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    config_dir = tmp_path / ".buildkite"
    config_dir.mkdir()
    (config_dir / "pipelinegen.toml").write_text('variant = "uv"\n')
    uploaded = []
    monkeypatch.setattr("sys.argv", ["pipelinegen", "--repo-root", str(tmp_path)])
    monkeypatch.setattr("bktools.idcat_pipeline.upload_pipeline", uploaded.append)

    main()

    captured = capsys.readouterr()
    assert captured.out == ""
    assert len(uploaded) == 1
    assert "uv run pytest" in uploaded[0]
