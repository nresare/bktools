import subprocess
from pathlib import Path

import pytest
from click.testing import CliRunner

from bktools import manifest_builder_on_checkout


def test_run_manifest_builder_on_checkout_clones_generates_commit_and_pushes(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    manifest_config = tmp_path / "conf"
    calls = []

    def fake_run(args: list[str], **kwargs) -> subprocess.CompletedProcess[str]:
        call = ("run", args, kwargs.get("cwd"), kwargs["check"])
        calls.append(call)
        return subprocess.CompletedProcess(args, 0)

    def fake_generate(
        config: Path, output: Path, create_commit: bool, vars_from: Path | None
    ) -> set[Path]:
        calls.append(("generate", config, output.name, create_commit, vars_from))
        return set()

    monkeypatch.setattr(manifest_builder_on_checkout.subprocess, "run", fake_run)
    monkeypatch.setattr(manifest_builder_on_checkout, "generate", fake_generate)

    output_dir = manifest_builder_on_checkout.run_manifest_builder_on_checkout(
        "https://github.com/nresare/manifests.git", manifest_config
    )

    assert output_dir == Path(calls[0][1][-1])
    assert calls == [
        (
            "run",
            [
                "git",
                "clone",
                "--depth",
                "1",
                "https://github.com/nresare/manifests.git",
                calls[0][1][-1],
            ],
            None,
            True,
        ),
        ("generate", manifest_config, "output", True, None),
        ("run", ["git", "push"], calls[2][2], True),
    ]
    assert Path(calls[0][1][-1]).name == "output"
    assert Path(calls[2][2]).name == "output"


def test_run_manifest_builder_on_checkout_can_skip_commit_and_push(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    manifest_config = tmp_path / "conf"
    calls = []

    def fake_run(args: list[str], **kwargs) -> subprocess.CompletedProcess[str]:
        calls.append(("run", args, kwargs.get("cwd"), kwargs["check"]))
        return subprocess.CompletedProcess(args, 0)

    def fake_generate(
        config: Path, output: Path, create_commit: bool, vars_from: Path | None
    ) -> set[Path]:
        calls.append(("generate", config, output.name, create_commit, vars_from))
        return set()

    monkeypatch.setattr(manifest_builder_on_checkout.subprocess, "run", fake_run)
    monkeypatch.setattr(manifest_builder_on_checkout, "generate", fake_generate)

    manifest_builder_on_checkout.run_manifest_builder_on_checkout(
        "https://github.com/nresare/manifests.git",
        manifest_config,
        create_commit=False,
    )

    assert calls == [
        (
            "run",
            [
                "git",
                "clone",
                "--depth",
                "1",
                "https://github.com/nresare/manifests.git",
                calls[0][1][-1],
            ],
            None,
            True,
        ),
        ("generate", manifest_config, "output", False, None),
    ]


def test_run_manifest_builder_on_checkout_injects_clone_token(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    manifest_config = tmp_path / "conf"
    calls = []

    def fake_run(args: list[str], **kwargs) -> subprocess.CompletedProcess[str]:
        calls.append(args)
        return subprocess.CompletedProcess(args, 0)

    def fake_generate(
        config: Path, output: Path, create_commit: bool, vars_from: Path | None
    ) -> set[Path]:
        return set()

    monkeypatch.setattr(manifest_builder_on_checkout.subprocess, "run", fake_run)
    monkeypatch.setattr(manifest_builder_on_checkout, "generate", fake_generate)

    manifest_builder_on_checkout.run_manifest_builder_on_checkout(
        "https://github.com/nresare/manifests.git",
        manifest_config,
        create_commit=False,
        clone_token="secret token",
    )

    assert calls[0][:5] == [
        "git",
        "clone",
        "--depth",
        "1",
        "https://x-access-token:secret%20token@github.com/nresare/manifests.git",
    ]


def test_run_manifest_builder_on_checkout_forwards_vars_from(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    manifest_config = tmp_path / "conf"
    generate_calls = []

    def fake_run(args: list[str], **kwargs) -> subprocess.CompletedProcess[str]:
        return subprocess.CompletedProcess(args, 0)

    def fake_generate(
        config: Path, output: Path, create_commit: bool, vars_from: Path | None
    ) -> set[Path]:
        generate_calls.append(vars_from)
        return set()

    monkeypatch.setattr(manifest_builder_on_checkout.subprocess, "run", fake_run)
    monkeypatch.setattr(manifest_builder_on_checkout, "generate", fake_generate)

    manifest_builder_on_checkout.run_manifest_builder_on_checkout(
        "https://github.com/nresare/manifests.git",
        manifest_config,
        create_commit=False,
        vars_from=Path("ci-vars.toml"),
    )

    assert generate_calls == [Path("ci-vars.toml")]


def test_run_manifest_builder_on_checkout_generates_into_subdir(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    manifest_config = tmp_path / "conf"
    generate_outputs = []

    def fake_run(args: list[str], **kwargs) -> subprocess.CompletedProcess[str]:
        return subprocess.CompletedProcess(args, 0)

    def fake_generate(
        config: Path, output: Path, create_commit: bool, vars_from: Path | None
    ) -> set[Path]:
        generate_outputs.append(output)
        return set()

    monkeypatch.setattr(manifest_builder_on_checkout.subprocess, "run", fake_run)
    monkeypatch.setattr(manifest_builder_on_checkout, "generate", fake_generate)

    result = manifest_builder_on_checkout.run_manifest_builder_on_checkout(
        "https://github.com/nresare/manifests.git",
        manifest_config,
        create_commit=False,
        output_subdir="platform-dev",
    )

    assert generate_outputs[0].name == "platform-dev"
    assert generate_outputs[0].parent.name == "output"
    assert result == generate_outputs[0]


def test_inject_clone_token_rejects_non_http_url() -> None:
    with pytest.raises(Exception, match="http"):
        manifest_builder_on_checkout.inject_clone_token(
            "git@github.com:nresare/manifests.git", "token"
        )


def test_manifest_builder_on_checkout_cli_requires_repo() -> None:
    result = CliRunner().invoke(manifest_builder_on_checkout.main)

    assert result.exit_code == 2
    assert "Missing option '--repo'" in result.output
