from pathlib import Path

from bktools.image_version_hash import docker_image_tag


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
