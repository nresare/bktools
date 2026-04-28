import subprocess
from pathlib import Path

import pytest

from bktools.image_version_hash import (
    docker_context_hash,
    docker_image_tag,
    version_from_tag,
)


def run_git(repo_root: Path, *args: str) -> None:
    subprocess.run(["git", *args], cwd=repo_root, check=True)


def commit_all(repo_root: Path, message: str) -> None:
    subprocess.run(["git", "add", "."], cwd=repo_root, check=True)
    subprocess.run(
        [
            "git",
            "-c",
            "user.name=Test User",
            "-c",
            "user.email=test@example.com",
            "commit",
            "-m",
            message,
        ],
        cwd=repo_root,
        check=True,
    )


@pytest.mark.parametrize(
    ("tag", "version"),
    [
        ("v0.1.0", "0.1.0"),
        ("v1.2.3", "1.2.3"),
        ("v10.20.30", "10.20.30"),
        ("0.1.0", None),
        ("vv0.1.0", None),
        ("v0.1", None),
        ("v0.1.0.0", None),
        ("v0.1.x", None),
        ("v01.2.3", None),
    ],
)
def test_version_from_tag_parses_v_prefixed_semver_tags(
    tag: str, version: str | None
) -> None:
    assert version_from_tag(tag) == version


def test_docker_image_tag_uses_cargo_metadata_and_context_hash(tmp_path: Path) -> None:
    (tmp_path / "Cargo.toml").write_text(
        '[package]\nname = "idcat"\nversion = "0.1.0"\n'
    )
    (tmp_path / "Dockerfile").write_text("FROM scratch\n")
    (tmp_path / ".dockerignore").write_text("ignored.txt\n")
    (tmp_path / "ignored.txt").write_text("ignored\n")

    tag = docker_image_tag(tmp_path)

    assert tag.startswith("idcat:0.1.0-")
    assert len(tag.removeprefix("idcat:0.1.0-")) == 8


def test_docker_context_hash_ignores_dockerignore_file_contents(
    tmp_path: Path,
) -> None:
    (tmp_path / "Dockerfile").write_text("FROM scratch\n")
    (tmp_path / "app.py").write_text("print('hello')\n")
    (tmp_path / ".dockerignore").write_text("ignored.txt\n")
    (tmp_path / "ignored.txt").write_text("ignored\n")

    original_hash = docker_context_hash(tmp_path)

    (tmp_path / ".dockerignore").write_text("# comment changed\nignored.txt\n")

    assert docker_context_hash(tmp_path) == original_hash


def test_docker_context_hash_changes_when_docker_build_input_changes(
    tmp_path: Path,
) -> None:
    (tmp_path / "Dockerfile").write_text("FROM scratch\n")
    (tmp_path / "app.py").write_text("print('hello')\n")

    original_hash = docker_context_hash(tmp_path)

    (tmp_path / "app.py").write_text("print('goodbye')\n")

    assert docker_context_hash(tmp_path) != original_hash


def test_docker_image_tag_uses_nearest_version_tag_without_cargo(
    tmp_path: Path,
) -> None:
    run_git(tmp_path, "init")
    (tmp_path / "pyproject.toml").write_text('[project]\nname = "havtorn"\n')
    (tmp_path / "Dockerfile").write_text("FROM scratch\n")
    commit_all(tmp_path, "initial")
    run_git(tmp_path, "tag", "v1.2.3")
    run_git(tmp_path, "tag", "not-a-version")

    (tmp_path / "app.py").write_text("print('hello')\n")
    commit_all(tmp_path, "add app")
    run_git(tmp_path, "tag", "v1.3.1")

    (tmp_path / "README.md").write_text("# test\n")
    commit_all(tmp_path, "add readme")

    tag = docker_image_tag(tmp_path)

    assert tag.startswith("havtorn:1.3.1-")
    assert len(tag.removeprefix("havtorn:1.3.1-")) == 8


def test_docker_image_tag_fails_descriptively_without_version_source(
    tmp_path: Path,
) -> None:
    (tmp_path / "Dockerfile").write_text("FROM scratch\n")

    with pytest.raises(SystemExit, match="could not determine base version"):
        docker_image_tag(tmp_path)
