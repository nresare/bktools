import json
import logging
import urllib.error
import urllib.request
from email.message import Message
from io import BytesIO

import pytest
from click import ClickException
from click.testing import CliRunner

from bktools.notify_relcoord import (
    build_change,
    main,
    normalize_container_image_repo,
    normalize_endpoint,
    post_change,
    print_event_stream,
    request_relcoord_token,
)


def test_build_change_uses_buildkite_metadata(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("BUILDKITE_COMMIT", "deadbeef")
    monkeypatch.setenv("BUILDKITE_REPO", "https://github.com/example/app.git")

    change = build_change(
        tag="0.1.0-deadbeef", container_image_repo="repo.noa.re/example-app/"
    )

    assert change.commit == "deadbeef"
    assert change.repo_url == "https://github.com/example/app.git"
    assert change.tag == "0.1.0-deadbeef"
    assert change.container_image_repo == "repo.noa.re/example-app"
    assert change.container_image == "repo.noa.re/example-app:0.1.0-deadbeef"


def test_build_change_removes_tag_from_container_image_repo(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("BUILDKITE_COMMIT", "deadbeef")
    monkeypatch.setenv("BUILDKITE_REPO", "https://github.com/example/app.git")

    change = build_change(
        tag="0.1.0-deadbeef",
        container_image_repo="repo.noa.re/example-app:0.1.0-deadbeef",
    )

    assert change.container_image_repo == "repo.noa.re/example-app"
    assert change.container_image == "repo.noa.re/example-app:0.1.0-deadbeef"


def test_normalize_container_image_repo_keeps_registry_port() -> None:
    assert (
        normalize_container_image_repo("localhost:5000/example-app:0.1.0")
        == "localhost:5000/example-app"
    )


def test_request_relcoord_token_uses_endpoint_as_audience(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    calls = []

    def fake_check_output(args: list[str], *, text: bool) -> str:
        calls.append((args, text))
        return "token\n"

    monkeypatch.setattr("subprocess.check_output", fake_check_output)

    assert request_relcoord_token("relcoord.example.com") == "token"
    assert calls == [
        (
            [
                "buildkite-agent",
                "oidc",
                "request-token",
                "--audience",
                "relcoord.example.com",
            ],
            True,
        )
    ]


def test_post_change_posts_json_to_relcoord(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("BUILDKITE_COMMIT", "deadbeef")
    monkeypatch.setenv("BUILDKITE_REPO", "https://github.com/example/app.git")
    change = build_change(
        tag="0.1.0-deadbeef", container_image_repo="repo.noa.re/example-app"
    )
    requests: list[urllib.request.Request] = []

    class FakeResponse:
        headers: dict[str, str] = {}

        def __enter__(self) -> "FakeResponse":
            return self

        def __exit__(self, *args: object) -> None:
            pass

        def read(self) -> bytes:
            return b""

    def fake_urlopen(request: urllib.request.Request) -> FakeResponse:
        requests.append(request)
        return FakeResponse()

    monkeypatch.setattr("urllib.request.urlopen", fake_urlopen)

    post_change("relcoord.example.com", "token", change)

    request = requests[0]
    assert request.full_url == "https://relcoord.example.com/v1/change"
    assert request.get_method() == "POST"
    assert request.headers == {
        "Authorization": "Bearer token",
        "Content-type": "application/json",
        "Accept": "text/event-stream",
    }
    assert isinstance(request.data, bytes)
    assert json.loads(request.data) == {
        "commit": "deadbeef",
        "config_repo": "https://github.com/example/app.git",
        "image_repo": "repo.noa.re/example-app",
        "tag": "0.1.0-deadbeef",
    }


def test_event_stream_prints_each_result_as_it_arrives(
    capsys: pytest.CaptureFixture[str],
) -> None:
    def lines():
        yield b": keep-alive\n"
        yield b"\n"
        yield b"event: accepted\r\n"
        yield b'data: {"commit":"deadbeef"}\r\n'
        yield b"\r\n"
        yield b"event: progress\n"
        yield b'data: {"phase":"generate",\n'
        yield b'data: "message":"generated files"}\n'
        yield b"\n"
        assert capsys.readouterr().out == (
            'accepted: {"commit":"deadbeef"}\n'
            'progress: {"phase":"generate",\n"message":"generated files"}\n'
        )
        yield b"event: complete\n"
        yield b'data: {"processed":true}\n'
        yield b"\n"

    print_event_stream(lines())
    assert capsys.readouterr().out == 'complete: {"processed":true}\n'


def test_event_stream_error_fails_request(
    capsys: pytest.CaptureFixture[str],
) -> None:
    with pytest.raises(ClickException, match="relcoord request failed"):
        print_event_stream(
            [
                b"event: error\n",
                b'data: {"status":502,"error":"git_transport_failed","message":"clone failed"}\n',
                b"\n",
            ]
        )
    assert '"message":"clone failed"' in capsys.readouterr().out


def test_post_change_reports_relcoord_error_response(
    caplog: pytest.LogCaptureFixture,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("BUILDKITE_COMMIT", "deadbeef")
    monkeypatch.setenv("BUILDKITE_REPO", "https://github.com/example/app.git")
    change = build_change(
        tag="0.1.0-deadbeef", container_image_repo="repo.noa.re/example-app"
    )

    def fake_urlopen(request: urllib.request.Request) -> None:
        raise urllib.error.HTTPError(
            request.full_url,
            500,
            "Internal Server Error",
            hdrs=Message(),
            fp=BytesIO(b'{"message":"not a known repository"}'),
        )

    monkeypatch.setattr("urllib.request.urlopen", fake_urlopen)

    with caplog.at_level(logging.ERROR), pytest.raises(ClickException) as exc_info:
        post_change("relcoord.example.com", "token", change)

    assert exc_info.value.message == "relcoord request failed"
    assert caplog.messages == [
        (
            "Failed to post to https://relcoord.example.com/v1/change. "
            "The endpoint returned 500: not a known repository"
        ),
        (
            'The following data was sent: {"commit": "deadbeef", '
            '"config_repo": "https://github.com/example/app.git", '
            '"image_repo": "repo.noa.re/example-app", '
            '"tag": "0.1.0-deadbeef"}'
        ),
    ]


def test_post_change_reports_raw_relcoord_error_response_when_not_json(
    caplog: pytest.LogCaptureFixture,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("BUILDKITE_COMMIT", "deadbeef")
    monkeypatch.setenv("BUILDKITE_REPO", "https://github.com/example/app.git")
    change = build_change(
        tag="0.1.0-deadbeef", container_image_repo="repo.noa.re/example-app"
    )

    def fake_urlopen(request: urllib.request.Request) -> None:
        raise urllib.error.HTTPError(
            request.full_url,
            502,
            "Bad Gateway",
            hdrs=Message(),
            fp=BytesIO(b"upstream failed"),
        )

    monkeypatch.setattr("urllib.request.urlopen", fake_urlopen)

    with caplog.at_level(logging.ERROR), pytest.raises(ClickException) as exc_info:
        post_change("relcoord.example.com", "token", change)

    assert exc_info.value.message == "relcoord request failed"
    assert (
        "Failed to post to https://relcoord.example.com/v1/change. "
        "The endpoint returned 502: upstream failed"
    ) in caplog.messages


def test_main_reports_relcoord_error_without_traceback(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("BUILDKITE_COMMIT", "deadbeef")
    monkeypatch.setenv("BUILDKITE_REPO", "https://github.com/example/app.git")
    monkeypatch.setattr(
        "bktools.notify_relcoord.request_relcoord_token", lambda endpoint: "token"
    )

    def fake_urlopen(request: urllib.request.Request) -> None:
        raise urllib.error.HTTPError(
            request.full_url,
            400,
            "Bad Request",
            hdrs=Message(),
            fp=BytesIO(b'{"message":"container image is invalid"}'),
        )

    monkeypatch.setattr("urllib.request.urlopen", fake_urlopen)

    result = CliRunner().invoke(
        main,
        [
            "https://relcoord.example.com/",
            "--tag",
            "0.1.0-deadbeef",
            "--repo",
            "repo.noa.re/example-app",
        ],
    )

    assert result.exit_code == 1
    assert "Traceback" not in result.output
    assert (
        "ERROR Failed to post to https://relcoord.example.com/v1/change. "
        "The endpoint returned 400: container image is invalid"
    ) in result.output
    assert "ERROR The following data was sent: " in result.output
    assert "Error: relcoord request failed" in result.output


def test_main_notifies_relcoord(monkeypatch: pytest.MonkeyPatch) -> None:
    posted = []
    monkeypatch.setenv("BUILDKITE_COMMIT", "deadbeef")
    monkeypatch.setenv("BUILDKITE_REPO", "https://github.com/example/app.git")
    monkeypatch.setattr(
        "bktools.notify_relcoord.request_relcoord_token", lambda endpoint: "token"
    )
    monkeypatch.setattr(
        "bktools.notify_relcoord.post_change",
        lambda endpoint, token, change: posted.append((endpoint, token, change)),
    )

    result = CliRunner().invoke(
        main,
        [
            "https://relcoord.example.com/",
            "--tag",
            "0.1.0-deadbeef",
            "--repo",
            "repo.noa.re/example-app",
        ],
    )

    assert result.exit_code == 0
    assert posted[0][0] == "relcoord.example.com"
    assert posted[0][1] == "token"
    assert posted[0][2].container_image == "repo.noa.re/example-app:0.1.0-deadbeef"


@pytest.mark.parametrize(
    ("options", "path", "pull_request"),
    [
        (["--system"], "/v1/change", None),
        (["--system", "--diffcomment"], "/v1/diffcomment", 42),
    ],
)
def test_main_sends_system_request(
    monkeypatch: pytest.MonkeyPatch,
    options: list[str],
    path: str,
    pull_request: int | None,
) -> None:
    monkeypatch.setenv("BUILDKITE_REPO", "https://github.com/acme/system.git")
    monkeypatch.setenv("BUILDKITE_COMMIT", "deadbeef")
    if pull_request is not None:
        monkeypatch.setenv("BUILDKITE_PULL_REQUEST", str(pull_request))
    monkeypatch.setattr(
        "bktools.notify_relcoord.request_relcoord_token", lambda endpoint: "token"
    )
    requests: list[urllib.request.Request] = []

    class FakeResponse:
        headers = {"Content-Type": "text/event-stream; charset=utf-8"}

        def __enter__(self) -> "FakeResponse":
            return self

        def __exit__(self, *args: object) -> None:
            pass

        def __iter__(self):
            yield b"event: accepted\n"
            yield b'data: {"commit":"deadbeef"}\n'
            yield b"\n"
            yield b"event: complete\n"
            yield b'data: {"processed":true}\n'
            yield b"\n"

    def fake_urlopen(request: urllib.request.Request) -> FakeResponse:
        requests.append(request)
        return FakeResponse()

    monkeypatch.setattr("urllib.request.urlopen", fake_urlopen)

    result = CliRunner().invoke(main, ["https://relcoord.example.com/", *options])

    assert result.exit_code == 0, result.output
    request = requests[0]
    assert request.full_url == f"https://relcoord.example.com{path}"
    assert request.get_header("Authorization") == "Bearer token"
    assert isinstance(request.data, bytes)
    assert json.loads(request.data) == {
        "config_repo": "https://github.com/acme/system.git",
        "commit": "deadbeef",
        "system": True,
        **({"pull_request": pull_request} if pull_request is not None else {}),
    }
    assert 'accepted: {"commit":"deadbeef"}' in result.output
    assert 'complete: {"processed":true}' in result.output


def test_system_diffcomment_requires_pull_request(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("BUILDKITE_REPO", "https://github.com/acme/system.git")
    monkeypatch.setenv("BUILDKITE_COMMIT", "deadbeef")
    monkeypatch.setenv("BUILDKITE_PULL_REQUEST", "false")

    result = CliRunner().invoke(
        main, ["relcoord.example.com", "--system", "--diffcomment"]
    )

    assert result.exit_code != 0
    assert "BUILDKITE_PULL_REQUEST must be a positive integer" in result.output


def test_system_request_reports_relcoord_error(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("BUILDKITE_REPO", "https://github.com/acme/system.git")
    monkeypatch.setenv("BUILDKITE_COMMIT", "deadbeef")
    monkeypatch.setattr(
        "bktools.notify_relcoord.request_relcoord_token", lambda endpoint: "token"
    )

    def reject(request: urllib.request.Request) -> None:
        raise urllib.error.HTTPError(
            request.full_url,
            403,
            "Forbidden",
            Message(),
            BytesIO(b'{"message":"role not permitted"}'),
        )

    monkeypatch.setattr("urllib.request.urlopen", reject)

    result = CliRunner().invoke(main, ["relcoord.example.com", "--system"])

    assert result.exit_code != 0
    assert "role not permitted" in result.output


def test_system_options_reject_image_arguments() -> None:
    result = CliRunner().invoke(
        main, ["relcoord.example.com", "--system", "--tag", "1.0"]
    )

    assert result.exit_code != 0
    assert "cannot be combined with --system" in result.output


def test_normalize_endpoint_removes_scheme_and_trailing_slash() -> None:
    assert normalize_endpoint("https://relcoord.example.com/") == "relcoord.example.com"
