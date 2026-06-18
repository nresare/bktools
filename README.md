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
  - `variant = "manifest-builder"`: use `repo` as the manifest output
    repository. On pull request builds, generate a diff comment through the
    `diffcomment` entrypoint. On other builds, clone the output
    repository, run `manifest_builder.generate()` with the current checkout as
    input and the cloned repository as output, create a manifest commit, and push
    it through the `manifest-builder-on-checkout` entrypoint.

    ```toml
    variant = "manifest-builder"
    repo = "https://github.com/example/manifests.git"
    ```
  - `output = "container"`: add a main-branch Docker publish step using
    OIDC registry login and `docker buildx build` with zstd-compressed image
    output.
  - `relcoord-endpoint = "relcoord.example.com"`: after a container image is
    built and pushed, call
    `uv run notify-relcoord relcoord.example.com --repo ... --tag ...`.
  - `variant = "rust-container"` is deprecated. Use `variant = "rust"` with
    `output = "container"` instead.
- `bktools-image-version-hash`: hash a Docker build context and optionally emit a
  Docker tag. The base version comes from `Cargo.toml` when present, otherwise
  from the nearest reachable `vX.Y.Z` git tag.
- `notify-relcoord`: notify a relcoord endpoint about a published container
  image. The tool takes the endpoint as a positional argument, requests a
  Buildkite OIDC token for that audience, and posts the current
  `BUILDKITE_COMMIT`, `BUILDKITE_REPO`, tag, and OCI image repository such as
  `repo.noa.re/idmouse` to `/v1/change` as `commit`, `config_repo`, `tag`, and
  `image_repo`.
