import json

import pytest
from click.testing import CliRunner

from bktools.notify_relcoord import (
    build_change,
    main,
    normalize_endpoint,
    post_change,
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
    requests = []

    class FakeResponse:
        def __enter__(self) -> "FakeResponse":
            return self

        def __exit__(self, *args: object) -> None:
            pass

        def read(self) -> bytes:
            return b""

    def fake_urlopen(request: object) -> FakeResponse:
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
    }
    assert json.loads(request.data) == {
        "commit": "deadbeef",
        "repo_url": "https://github.com/example/app.git",
        "tag": "0.1.0-deadbeef",
        "container_image_repo": "repo.noa.re/example-app",
        "container_image": "repo.noa.re/example-app:0.1.0-deadbeef",
    }


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


def test_normalize_endpoint_removes_scheme_and_trailing_slash() -> None:
    assert normalize_endpoint("https://relcoord.example.com/") == "relcoord.example.com"
