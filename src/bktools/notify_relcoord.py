from __future__ import annotations

import json
import logging
import os
import subprocess
import sys
import urllib.error
import urllib.request
from collections.abc import Iterable, Mapping
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
@click.option("--tag", help="Container image tag that was published.")
@click.option("--repo", help="Container image repository.")
@click.option(
    "--system", is_flag=True, help="Generate system manifests through relcoord."
)
@click.option(
    "--diffcomment", is_flag=True, help="Comment on the current pull request."
)
def main(
    endpoint: str, tag: str | None, repo: str | None, system: bool, diffcomment: bool
) -> None:
    logging.basicConfig(
        format="%(asctime)s %(levelname)s %(message)s",
        datefmt="%Y-%m-%d %H:%M:%S",
        level=logging.INFO,
        stream=sys.stderr,
        force=True,
    )

    endpoint = normalize_endpoint(endpoint)
    if diffcomment and not system:
        raise click.UsageError("--diffcomment requires --system")
    if system:
        if tag is not None or repo is not None:
            raise click.UsageError("--tag and --repo cannot be combined with --system")
        mode = "diffcomment" if diffcomment else "change"
        payload = build_system_request(mode)
        logger.info("requesting relcoord token for %s", endpoint)
        token = request_relcoord_token(endpoint)
        post_request(endpoint, mode, token, payload)
        logger.info("relcoord system %s completed", mode)
        return

    if tag is None or repo is None:
        raise click.UsageError("--tag and --repo are required unless --system is set")
    logger.info("requesting relcoord token for %s", endpoint)
    token = request_relcoord_token(endpoint)

    change = build_change(tag=tag, container_image_repo=repo)
    logger.info("notifying relcoord about %s", change.container_image)
    post_change(endpoint, token, change)
    logger.info("notified relcoord")


def build_system_request(mode: str) -> dict[str, str | int | bool]:
    repo = os.environ.get("BUILDKITE_REPO", "").strip()
    commit = os.environ.get("BUILDKITE_COMMIT", "").strip()
    if not repo:
        raise click.ClickException("BUILDKITE_REPO is required")
    if not commit:
        raise click.ClickException("BUILDKITE_COMMIT is required")

    payload: dict[str, str | int | bool] = {
        "config_repo": repo,
        "commit": commit,
        "system": True,
    }
    if mode == "diffcomment":
        pull_request = os.environ.get("BUILDKITE_PULL_REQUEST", "")
        if not pull_request.isdecimal() or int(pull_request) <= 0:
            raise click.ClickException(
                "BUILDKITE_PULL_REQUEST must be a positive integer"
            )
        payload["pull_request"] = int(pull_request)
    return payload


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

    image_repo = normalize_container_image_repo(container_image_repo)
    return RelcoordChange(
        commit=commit,
        repo_url=repo_url,
        tag=tag,
        container_image_repo=image_repo,
        container_image=f"{image_repo}:{tag}",
    )


def normalize_container_image_repo(container_image_repo: str) -> str:
    image_repo = container_image_repo.rstrip("/")
    last_slash = image_repo.rfind("/")
    last_colon = image_repo.rfind(":")
    if last_colon > last_slash:
        return image_repo[:last_colon]
    return image_repo


def post_change(endpoint: str, token: str, change: RelcoordChange) -> None:
    payload = {
        "commit": change.commit,
        "config_repo": change.repo_url,
        "image_repo": change.container_image_repo,
        "tag": change.tag,
    }
    post_request(endpoint, "change", token, payload)


def post_request(
    endpoint: str, mode: str, token: str, payload: Mapping[str, str | int | bool]
) -> None:
    url = f"https://{endpoint}/v1/{mode}"
    body = json.dumps(payload).encode()
    request = urllib.request.Request(
        url,
        data=body,
        method="POST",
        headers={
            "Authorization": f"Bearer {token}",
            "Content-Type": "application/json",
            "Accept": "text/event-stream",
        },
    )
    try:
        with urllib.request.urlopen(request) as response:
            content_type = response.headers.get("Content-Type", "")
            if (
                content_type.split(";", maxsplit=1)[0].strip().lower()
                == "text/event-stream"
            ):
                print_event_stream(response)
            else:
                body = response.read().decode("utf-8", errors="replace")
                if body:
                    click.echo(body)
    except urllib.error.HTTPError as error:
        response_body = error.read().decode("utf-8", errors="replace")
        response_message = relcoord_error_message(response_body)
        serialized_payload = json.dumps(payload, sort_keys=True)
        logger.error(
            "Failed to post to %s. The endpoint returned %s: %s",
            url,
            error.code,
            response_message,
        )
        logger.error("The following data was sent: %s", serialized_payload)
        raise click.ClickException("relcoord request failed") from None


def print_event_stream(lines: Iterable[bytes]) -> None:
    event: str | None = None
    data: list[str] = []
    finished = False

    for raw_line in lines:
        line = raw_line.decode("utf-8", errors="replace").rstrip("\r\n")
        if not line:
            if event is not None and data:
                payload = "\n".join(data)
                click.echo(f"{event}: {payload}")
                if event == "error":
                    raise click.ClickException("relcoord request failed")
                if event == "complete":
                    finished = True
            event = None
            data = []
        elif line.startswith("event:"):
            event = line.removeprefix("event:").lstrip()
        elif line.startswith("data:"):
            data.append(line.removeprefix("data:").lstrip())

    if not finished:
        raise click.ClickException("relcoord response ended before completion")


def relcoord_error_message(response_body: str) -> str:
    try:
        response_json = json.loads(response_body)
    except json.JSONDecodeError:
        return response_body

    if isinstance(response_json, dict) and isinstance(
        response_json.get("message"), str
    ):
        return response_json["message"]
    return response_body


if __name__ == "__main__":
    main()
