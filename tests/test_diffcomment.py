import pytest
import subprocess
from pathlib import Path

from click.testing import CliRunner

from bktools import diffcomment


@pytest.fixture(autouse=True)
def clean_buildkite_env(monkeypatch: pytest.MonkeyPatch) -> None:
    for name in (
        "BUILDKITE_PULL_REQUEST",
        "BUILDKITE_PULL_REQUEST_REPO",
        "BUILDKITE_REPO",
        "BUILDKITE_BUILD_URL",
        "BUILDKITE_COMMIT",
        "BKTOOLS_GITHUB_PROXY_AUDIENCE",
        "BKTOOLS_GITHUB_PROXY_BASE_URL",
    ):
        monkeypatch.delenv(name, raising=False)


def test_main_skips_non_pull_request(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("BUILDKITE_PULL_REQUEST", "false")

    result = CliRunner().invoke(diffcomment.main)

    assert result.exit_code == 0
    assert "skipping manifest diff comment" in result.stderr


def test_main_posts_comment_for_pull_request(monkeypatch: pytest.MonkeyPatch) -> None:
    posted = []
    monkeypatch.setenv("BUILDKITE_PULL_REQUEST", "42")
    monkeypatch.setenv("BUILDKITE_REPO", "git@github.com:nresare/berries-config.git")
    monkeypatch.setattr(
        diffcomment,
        "run_manifest_builder_diff",
        lambda target_repository: (7, "diff"),
    )
    monkeypatch.setattr(diffcomment, "request_github_proxy_token", lambda: "token")
    monkeypatch.setattr(
        diffcomment,
        "post_issue_comment",
        lambda token, owner, repo, pr_number, body: posted.append(
            (token, owner, repo, pr_number, body)
        ),
    )

    result = CliRunner().invoke(
        diffcomment.main,
        ["--target-repository", "https://github.com/nresare/manifests.git"],
    )

    assert result.exit_code == 7
    assert posted == [
        (
            "token",
            "nresare",
            "berries-config",
            "42",
            "### `manifest-builder diff`\n\n"
            "Pull request: #42\n"
            "Exit code: `7`\n\n"
            "```diff\n"
            "diff\n"
            "```",
        )
    ]


def test_main_requires_target_repository_for_pull_request(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("BUILDKITE_PULL_REQUEST", "42")

    result = CliRunner().invoke(diffcomment.main)

    assert result.exit_code == 2
    assert "requires --target-repository" in result.output


def test_run_manifest_builder_diff_clones_target_and_calls_show_diff(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    manifest_config = tmp_path / "conf"
    calls = []

    def fake_run(args: list[str], *, check: bool) -> subprocess.CompletedProcess[str]:
        calls.append(("run", args[:-1], Path(args[-1]).name, check))
        return subprocess.CompletedProcess(args, 0)

    def fake_show_diff(config: Path, output: Path) -> str:
        calls.append(("show_diff", [str(config), output.name], True))
        return "diff output\n"

    monkeypatch.setattr(diffcomment.subprocess, "run", fake_run)
    monkeypatch.setattr(diffcomment, "manifest_builder_show_diff", fake_show_diff)

    assert diffcomment.run_manifest_builder_diff(
        target_repository="https://github.com/nresare/manifests.git",
        manifest_config_dir=manifest_config,
    ) == (0, "diff output\n")
    assert calls == [
        (
            "run",
            [
                "git",
                "clone",
                "--depth",
                "1",
                "https://github.com/nresare/manifests.git",
            ],
            "output",
            True,
        ),
        ("show_diff", [str(manifest_config), "output"], True),
    ]


def test_github_repo_parses_buildkite_repo(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("BUILDKITE_PULL_REQUEST_REPO", raising=False)
    monkeypatch.setenv(
        "BUILDKITE_REPO", "https://github.com/nresare/repo.with.dots.git"
    )

    assert diffcomment.github_repo() == ("nresare", "repo.with.dots")


def test_github_repo_prefers_pull_request_repo(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("BUILDKITE_PULL_REQUEST_REPO", "git@github.com:fork/source.git")
    monkeypatch.setenv("BUILDKITE_REPO", "https://github.com/nresare/base.git")

    assert diffcomment.github_repo() == ("fork", "source")


def test_build_comment_body_includes_build_metadata(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("BUILDKITE_BUILD_URL", "https://buildkite.example/build")
    monkeypatch.setenv("BUILDKITE_COMMIT", "abc123")

    body = diffcomment.build_comment_body("12", 0, "```diff\n+hello\n```")

    assert "Pull request: #12" in body
    assert "Build: https://buildkite.example/build" in body
    assert "Commit: `abc123`" in body
    assert "````diff\n```diff\n+hello\n```\n````" in body


def test_build_comment_body_uses_placeholder_for_empty_output() -> None:
    body = diffcomment.build_comment_body("12", 0, "")

    assert "No diff output produced." in body


def test_truncate_comment_limits_body_size() -> None:
    body = diffcomment.truncate_comment("x" * (diffcomment.MAX_COMMENT_BYTES + 1))

    assert len(body.encode()) <= diffcomment.MAX_COMMENT_BYTES
    assert body.endswith(
        "_Output truncated to fit within the GitHub comment size limit._"
    )
