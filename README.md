# Buildkite Tools

Small Python tools intended to be invoked from Buildkite pipelines.

## Development

This project uses [uv](https://docs.astral.sh/uv/) for development. To set up the
environment, run `uv sync`. Tests and checks can be run with:

```bash
uv run ruff check
uv run ruff format --check
uv run ty check
uv run pytest
```

## Tools

- `pipelinegen`: generate Buildkite pipeline YAML from `.buildkite/pipelinegen.toml`,
  write it to `pipeline.yaml`, upload that file as a Buildkite artifact, and
  upload it with `buildkite-agent pipeline upload`. Use `--dump` to write the
  generated YAML to stdout instead.
  - `variant = "rust"`: Rust build/test.
  - `variant = "uv"`: uv/ruff/pytest/build/ty checks plus a main-branch Python
    package publish step.
  - `output = "container"`: add a main-branch Docker publish step using
    `docker buildx build` and `docker-image-push`.
  - `variant = "rust-container"` is deprecated. Use `variant = "rust"` with
    `output = "container"` instead.
- `bktools-image-version-hash`: hash a Docker build context and optionally emit a
  Docker tag. The base version comes from `Cargo.toml` when present, otherwise
  from the nearest reachable `vX.Y.Z` git tag.
