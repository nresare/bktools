from __future__ import annotations

import json
import logging
import os
import subprocess
import sys
import urllib.request
from dataclasses import dataclass

import click

logger = logging.getLogger("notify-relcoord")


@dataclass(frozen=True)
class RelcoordChange:
    commit: str
    repo_url: str
    tag: str
    container_image_repo: str
    container_image: str


@click.command()
@click.argument("endpoint")
@click.option("--tag", required=True, help="Container image tag that was published.")
@click.option("--repo", required=True, help="Container image repository.")
def main(endpoint: str, tag: str, repo: str) -> None:
    logging.basicConfig(
        format="%(asctime)s %(levelname)s %(message)s",
        datefmt="%Y-%m-%d %H:%M:%S",
        level=logging.INFO,
        stream=sys.stderr,
        force=True,
    )

    endpoint = normalize_endpoint(endpoint)
    logger.info("requesting relcoord token for %s", endpoint)
    token = request_relcoord_token(endpoint)

    change = build_change(tag=tag, container_image_repo=repo)
    logger.info("notifying relcoord about %s", change.container_image)
    post_change(endpoint, token, change)
    logger.info("notified relcoord")


def normalize_endpoint(endpoint: str) -> str:
    endpoint = endpoint.removeprefix("https://").removeprefix("http://")
    return endpoint.rstrip("/")


def request_relcoord_token(endpoint: str) -> str:
    return subprocess.check_output(
        ["buildkite-agent", "oidc", "request-token", "--audience", endpoint],
        text=True,
    ).strip()


def build_change(tag: str, container_image_repo: str) -> RelcoordChange:
    commit = os.environ.get("BUILDKITE_COMMIT", "").strip()
    if not commit:
        raise click.ClickException("BUILDKITE_COMMIT is required")

    repo_url = os.environ.get("BUILDKITE_REPO", "").strip()
    if not repo_url:
        raise click.ClickException("BUILDKITE_REPO is required")

    image_repo = container_image_repo.rstrip("/")
    return RelcoordChange(
        commit=commit,
        repo_url=repo_url,
        tag=tag,
        container_image_repo=image_repo,
        container_image=f"{image_repo}:{tag}",
    )


def post_change(endpoint: str, token: str, change: RelcoordChange) -> None:
    body = json.dumps(
        {
            "commit": change.commit,
            "repo_url": change.repo_url,
            "tag": change.tag,
            "container_image_repo": change.container_image_repo,
            "container_image": change.container_image,
        }
    ).encode()
    request = urllib.request.Request(
        f"https://{endpoint}/v1/change",
        data=body,
        method="POST",
        headers={
            "Authorization": f"Bearer {token}",
            "Content-Type": "application/json",
        },
    )
    with urllib.request.urlopen(request) as response:
        response.read()


if __name__ == "__main__":
    main()
