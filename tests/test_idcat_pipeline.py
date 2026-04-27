from bktools.idcat_pipeline import pipeline_yaml, uv_pipeline_yaml


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
