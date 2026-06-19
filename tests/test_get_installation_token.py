import urllib.error
import urllib.request
from email.message import Message
from io import BytesIO

import pytest
from click.testing import CliRunner

from bktools.get_installation_token import (
    endpoint_audience,
    main,
    normalize_endpoint,
    parse_owner_repo,
    request_installation_token,
    request_pipeline_oidc_token,
)


def test_normalize_endpoint_strips_trailing_slash() -> None:
    assert normalize_endpoint("https://idcat.noa.re/") == "https://idcat.noa.re"


def test_endpoint_audience_strips_scheme() -> None:
    assert endpoint_audience("https://idcat.noa.re") == "idcat.noa.re"


def test_parse_owner_repo_handles_https_and_ssh_urls() -> None:
    assert (
        parse_owner_repo("https://github.com/nresare/berries-config.git")
        == "nresare/berries-config"
    )
    assert (
        parse_owner_repo("git@github.com:nresare/berries-config.git")
        == "nresare/berries-config"
    )


def test_parse_owner_repo_rejects_unrecognized_url() -> None:
    with pytest.raises(Exception, match="could not infer GitHub owner/repo"):
        parse_owner_repo("https://example.com/not-a-repo")


def test_request_pipeline_oidc_token_uses_audience(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    calls = []

    def fake_check_output(args: list[str], *, text: bool) -> str:
        calls.append((args, text))
        return "oidc-token\n"

    monkeypatch.setattr("subprocess.check_output", fake_check_output)

    assert request_pipeline_oidc_token("idcat.noa.re") == "oidc-token"
    assert calls == [
        (
            [
                "buildkite-agent",
                "oidc",
                "request-token",
                "--audience",
                "idcat.noa.re",
            ],
            True,
        )
    ]


def test_request_installation_token_gets_token(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    requests: list[urllib.request.Request] = []

    class FakeResponse:
        def __enter__(self) -> "FakeResponse":
            return self

        def __exit__(self, *args: object) -> None:
            pass

        def read(self) -> bytes:
            return b"ghs_installationtoken\n"

    def fake_urlopen(request: urllib.request.Request, timeout: int) -> FakeResponse:
        requests.append(request)
        return FakeResponse()

    monkeypatch.setattr("urllib.request.urlopen", fake_urlopen)

    token = request_installation_token(
        "https://idcat.noa.re", "berries-app", "nresare/manifests", "oidc-token"
    )

    assert token == "ghs_installationtoken"
    assert len(requests) == 1
    request = requests[0]
    assert request.get_method() == "POST"
    assert (
        request.full_url
        == "https://idcat.noa.re/installation-token/berries-app/nresare/manifests"
    )
    assert request.headers["Authorization"] == "Bearer oidc-token"


def test_request_installation_token_raises_on_http_error(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    def fake_urlopen(request: urllib.request.Request, timeout: int) -> object:
        raise urllib.error.HTTPError(
            request.full_url,
            403,
            "Forbidden",
            Message(),
            BytesIO(b"no access"),
        )

    monkeypatch.setattr("urllib.request.urlopen", fake_urlopen)

    with pytest.raises(Exception, match="installation token request failed"):
        request_installation_token(
            "https://idcat.noa.re", "berries-app", "nresare/manifests", "oidc-token"
        )


def test_main_prints_installation_token_to_stdout(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr("subprocess.check_output", lambda args, *, text: "oidc-token\n")

    class FakeResponse:
        def __enter__(self) -> "FakeResponse":
            return self

        def __exit__(self, *args: object) -> None:
            pass

        def read(self) -> bytes:
            return b"ghs_installationtoken"

    monkeypatch.setattr(
        "urllib.request.urlopen", lambda request, timeout: FakeResponse()
    )

    result = CliRunner().invoke(
        main,
        [
            "--endpoint",
            "https://idcat.noa.re/",
            "--github-app",
            "nresare-buildsystem",
            "--repo",
            "https://github.com/nresare/manifests.git",
        ],
    )

    assert result.exit_code == 0
    assert result.stdout == "ghs_installationtoken\n"
