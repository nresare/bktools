from __future__ import annotations

import logging
import subprocess
import sys
import tempfile
import urllib.parse
from pathlib import Path

import click

# The public ``manifest_builder.generate`` wrapper does not forward ``vars_from``,
# so use the api-level entry point that does.
from manifest_builder.api import generate

logger = logging.getLogger("manifest-builder-on-checkout")


@click.command()
@click.option(
    "--repo",
    required=True,
    help="Repository to shallow-clone as the manifest output before generation",
)
@click.option(
    "--commit/--no-commit",
    default=True,
    help="Create and push a manifest commit in the output checkout",
)
def main(repo: str, commit: bool) -> None:
    logging.basicConfig(
        format="%(asctime)s %(levelname)s %(message)s",
        datefmt="%Y-%m-%d %H:%M:%S",
        level=logging.INFO,
        stream=sys.stderr,
        force=True,
    )

    output_dir = run_manifest_builder_on_checkout(repo, create_commit=commit)
    click.echo(output_dir)


def run_manifest_builder_on_checkout(
    repo: str,
    manifest_config_dir: Path = Path("."),
    *,
    create_commit: bool = True,
    clone_token: str | None = None,
    vars_from: Path | None = None,
) -> Path:
    tmpdir = tempfile.mkdtemp(prefix="bktools-manifest-builder-")
    output_dir = Path(tmpdir) / "output"
    clone_output_repository(repo, output_dir, clone_token=clone_token)
    logger.info("generating manifests from %s into %s", manifest_config_dir, output_dir)
    generate(
        manifest_config_dir,
        output_dir,
        create_commit=create_commit,
        vars_from=vars_from,
    )
    if create_commit:
        push_output_repository(output_dir)
    return output_dir


def clone_output_repository(
    repo: str, output_dir: Path, *, clone_token: str | None = None
) -> None:
    logger.info("cloning output repository %s to %s", repo, output_dir)
    clone_url = inject_clone_token(repo, clone_token) if clone_token else repo
    subprocess.run(
        ["git", "clone", "--depth", "1", clone_url, str(output_dir)], check=True
    )


def inject_clone_token(repo: str, token: str) -> str:
    parts = urllib.parse.urlsplit(repo)
    if parts.scheme not in ("http", "https") or not parts.hostname:
        raise click.ClickException(
            f"a clone token requires an http(s) repository URL, got: {repo!r}"
        )
    netloc = f"x-access-token:{urllib.parse.quote(token, safe='')}@{parts.hostname}"
    if parts.port is not None:
        netloc += f":{parts.port}"
    return urllib.parse.urlunsplit(
        (parts.scheme, netloc, parts.path, parts.query, parts.fragment)
    )


def push_output_repository(output_dir: Path) -> None:
    logger.info("pushing generated manifests from %s", output_dir)
    subprocess.run(["git", "push"], cwd=output_dir, check=True)


if __name__ == "__main__":
    main()
