from __future__ import annotations

import logging
import re
import subprocess
import sys
import urllib.error
import urllib.request

import click

logger = logging.getLogger("get-installation-token")


@click.command()
@click.option(
    "--endpoint",
    required=True,
    help="idcat endpoint to request the installation token from.",
)
@click.option(
    "--github-app",
    required=True,
    help="GitHub App that idcat should mint the installation token for.",
)
@click.option(
    "--repo",
    required=True,
    help=(
        "Repository URL (e.g. $BUILDKITE_REPO) the installation token should be "
        "valid for. The OWNER/REPO is derived from this URL."
    ),
)
def main(endpoint: str, github_app: str, repo: str) -> None:
    logging.basicConfig(
        format="%(asctime)s %(levelname)s %(message)s",
        datefmt="%Y-%m-%d %H:%M:%S",
        level=logging.INFO,
        stream=sys.stderr,
        force=True,
    )

    endpoint = normalize_endpoint(endpoint)
    audience = endpoint_audience(endpoint)
    owner_repo = parse_owner_repo(repo)

    logger.info("requesting Buildkite OIDC token for audience %s", audience)
    oidc_token = request_pipeline_oidc_token(audience)

    logger.info(
        "requesting installation token for %s via app %s from %s",
        owner_repo,
        github_app,
        endpoint,
    )
    installation_token = request_installation_token(
        endpoint, github_app, owner_repo, oidc_token
    )
    logger.info("obtained installation token for %s", owner_repo)

    click.echo(installation_token)


def normalize_endpoint(endpoint: str) -> str:
    return endpoint.rstrip("/")


def endpoint_audience(endpoint: str) -> str:
    return endpoint.removeprefix("https://").removeprefix("http://")


def parse_owner_repo(repo_url: str) -> str:
    match = re.search(r"github\.com[:/]([^/]+)/([^/]+?)(?:\.git)?/?$", repo_url)
    if not match:
        raise click.ClickException(
            f"could not infer GitHub owner/repo from repository URL: {repo_url!r}"
        )
    return f"{match.group(1)}/{match.group(2)}"


def request_pipeline_oidc_token(audience: str) -> str:
    return subprocess.check_output(
        ["buildkite-agent", "oidc", "request-token", "--audience", audience],
        text=True,
    ).strip()


def request_installation_token(
    endpoint: str, github_app: str, owner_repo: str, oidc_token: str
) -> str:
    url = f"{endpoint}/installation-token/{github_app}/{owner_repo}"
    request = urllib.request.Request(
        url,
        method="POST",
        headers={"Authorization": f"Bearer {oidc_token}"},
    )
    try:
        with urllib.request.urlopen(request, timeout=30) as response:
            return response.read().decode().strip()
    except urllib.error.HTTPError as error:
        response_body = error.read().decode("utf-8", errors="replace")
        logger.error(
            "failed to request installation token from %s, endpoint returned %s: %s",
            url,
            error.code,
            response_body,
        )
        raise click.ClickException("installation token request failed") from None


if __name__ == "__main__":
    main()
