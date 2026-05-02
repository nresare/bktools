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
        lambda target_repository: (
            7,
            diffcomment.ManifestDiff(
                stat="berries.yaml | 2 +-\n"
                " 1 file changed, 1 insertion(+), 1 deletion(-)\n",
                diff="diff",
            ),
        ),
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
            "```\n"
            "berries.yaml | 2 +-\n"
            " 1 file changed, 1 insertion(+), 1 deletion(-)\n"
            "```\n\n"
            "```diff\n"
            "diff\n"
            "```\n\n"
            f"manifest-builder version: `{diffcomment.MANIFEST_BUILDER_VERSION}`\n"
            "Exit code: `7`",
        )
    ]


def test_main_dumps_comment_body_without_posting(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    posted = []
    monkeypatch.setenv("BUILDKITE_PULL_REQUEST", "42")
    monkeypatch.setattr(
        diffcomment,
        "run_manifest_builder_diff",
        lambda target_repository: (
            0,
            diffcomment.ManifestDiff(stat="berries.yaml | 1 +\n", diff="diff"),
        ),
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
        [
            "--dump",
            "--target-repository",
            "https://github.com/nresare/manifests.git",
        ],
    )

    assert result.exit_code == 0
    assert result.stdout == (
        "```\n"
        "berries.yaml | 1 +\n"
        "```\n\n"
        "```diff\n"
        "diff\n"
        "```\n\n"
        f"manifest-builder version: `{diffcomment.MANIFEST_BUILDER_VERSION}`\n"
    )
    assert posted == []


def test_main_uploads_full_diff_artifact_when_context_diff_is_omitted(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    posted = []
    artifacts = []
    monkeypatch.chdir(tmp_path)
    monkeypatch.setenv("BUILDKITE_PULL_REQUEST", "42")
    monkeypatch.setenv("BUILDKITE_REPO", "git@github.com:nresare/berries-config.git")
    monkeypatch.setattr(
        diffcomment,
        "run_manifest_builder_diff",
        lambda target_repository: (
            0,
            diffcomment.ManifestDiff(
                stat="berries.yaml | 1 +\n",
                diff="x" * diffcomment.MAX_COMMENT_CHARS,
            ),
        ),
    )
    monkeypatch.setattr(diffcomment, "request_github_proxy_token", lambda: "token")
    monkeypatch.setattr(
        diffcomment,
        "post_issue_comment",
        lambda token, owner, repo, pr_number, body: posted.append(
            (token, owner, repo, pr_number, body)
        ),
    )
    monkeypatch.setattr(
        diffcomment,
        "upload_full_diff_artifact",
        lambda repo_root: artifacts.append(repo_root),
    )

    result = CliRunner().invoke(
        diffcomment.main,
        ["--target-repository", "https://github.com/nresare/manifests.git"],
    )

    artifact_path = tmp_path / diffcomment.FULL_DIFF_ARTIFACT
    assert result.exit_code == 0
    assert artifact_path.read_text().startswith("berries.yaml | 1 +\n\n")
    assert artifacts == [tmp_path]
    assert diffcomment.FULL_DIFF_ARTIFACT in posted[0][4]
    assert "```diff" not in posted[0][4]
    assert posted[0][4].startswith("```\nberries.yaml | 1 +\n```")


def test_main_dump_works_outside_pull_request(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(
        diffcomment,
        "run_manifest_builder_diff",
        lambda target_repository: (
            0,
            diffcomment.ManifestDiff(stat="berries.yaml | 1 +\n", diff="diff"),
        ),
    )

    result = CliRunner().invoke(
        diffcomment.main,
        [
            "--dump",
            "--target-repository",
            "https://github.com/nresare/manifests.git",
        ],
    )

    assert result.exit_code == 0
    assert "Pull request:" not in result.stdout
    assert "```diff\ndiff\n```" in result.stdout


def test_main_requires_target_repository_for_pull_request(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("BUILDKITE_PULL_REQUEST", "42")

    result = CliRunner().invoke(diffcomment.main)

    assert result.exit_code == 2
    assert "requires --target-repository" in result.output


def test_run_manifest_builder_diff_clones_target_generates_and_captures_git_diff(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    manifest_config = tmp_path / "conf"
    calls = []

    def fake_run(args: list[str], *, check: bool) -> subprocess.CompletedProcess[str]:
        calls.append(("run", args[:-1], Path(args[-1]).name, check))
        return subprocess.CompletedProcess(args, 0)

    def fake_generate(config: Path, output: Path) -> set[Path]:
        calls.append(("generate", [str(config), output.name], True))
        return set()

    def fake_diff_stat(output: Path) -> str:
        calls.append(("diff_stat", output.name))
        return "diff stat\n"

    def fake_diff(output: Path) -> str:
        calls.append(("diff", output.name))
        return "diff output\n"

    monkeypatch.setattr(diffcomment.subprocess, "run", fake_run)
    monkeypatch.setattr(diffcomment, "generate", fake_generate)
    monkeypatch.setattr(diffcomment, "git_diff_stat", fake_diff_stat)
    monkeypatch.setattr(diffcomment, "git_diff", fake_diff)

    assert diffcomment.run_manifest_builder_diff(
        target_repository="https://github.com/nresare/manifests.git",
        manifest_config_dir=manifest_config,
    ) == (0, diffcomment.ManifestDiff(stat="diff stat\n", diff="diff output\n"))
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
        ("generate", [str(manifest_config), "output"], True),
        ("diff_stat", "output"),
        ("diff", "output"),
    ]


def test_run_manifest_builder_diff_uses_current_directory_by_default(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    calls = []

    def fake_run(args: list[str], *, check: bool) -> subprocess.CompletedProcess[str]:
        calls.append(("run", args[:-1], Path(args[-1]).name, check))
        return subprocess.CompletedProcess(args, 0)

    def fake_generate(config: Path, output: Path) -> set[Path]:
        calls.append(("generate", config, output.name))
        return set()

    def fake_diff_stat(output: Path) -> str:
        calls.append(("diff_stat", output.name))
        return "diff stat\n"

    def fake_diff(output: Path) -> str:
        calls.append(("diff", output.name))
        return "diff output\n"

    monkeypatch.setattr(diffcomment.subprocess, "run", fake_run)
    monkeypatch.setattr(diffcomment, "generate", fake_generate)
    monkeypatch.setattr(diffcomment, "git_diff_stat", fake_diff_stat)
    monkeypatch.setattr(diffcomment, "git_diff", fake_diff)

    assert diffcomment.run_manifest_builder_diff(
        target_repository="https://github.com/nresare/manifests.git"
    ) == (0, diffcomment.ManifestDiff(stat="diff stat\n", diff="diff output\n"))
    assert calls[-3:] == [
        ("generate", Path("."), "output"),
        ("diff_stat", "output"),
        ("diff", "output"),
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


def test_build_comment_body_excludes_build_metadata_and_includes_version(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("BUILDKITE_BUILD_URL", "https://buildkite.example/build")
    monkeypatch.setenv("BUILDKITE_COMMIT", "abc123")

    comment = diffcomment.build_comment_body(
        "12",
        0,
        diffcomment.ManifestDiff(stat="file.yaml | 1 +\n", diff="```diff\n+hello\n```"),
    )

    assert "Pull request:" not in comment.body
    assert "### `manifest-builder diff`" not in comment.body
    assert "Build: https://buildkite.example/build" not in comment.body
    assert "Commit: `abc123`" not in comment.body
    assert "file.yaml | 1 +" in comment.body
    assert "````diff\n```diff\n+hello\n```\n````" in comment.body
    assert (
        f"manifest-builder version: `{diffcomment.MANIFEST_BUILDER_VERSION}`"
        in comment.body
    )
    assert not comment.omitted_context_diff


def test_build_comment_body_uses_placeholder_for_empty_output() -> None:
    comment = diffcomment.build_comment_body(
        "12", 0, diffcomment.ManifestDiff(stat="", diff="")
    )

    assert (
        "The generated output is the same before and after this change" in comment.body
    )
    assert (
        f"manifest-builder version: `{diffcomment.MANIFEST_BUILDER_VERSION}`"
        in comment.body
    )
    assert not comment.omitted_context_diff


def test_build_comment_body_omits_context_diff_when_too_large() -> None:
    comment = diffcomment.build_comment_body(
        "12",
        0,
        diffcomment.ManifestDiff(
            stat="file.yaml | 1 +\n",
            diff="x" * diffcomment.MAX_COMMENT_CHARS,
        ),
    )

    assert "file.yaml | 1 +" in comment.body
    assert "```diff" not in comment.body
    assert diffcomment.FULL_DIFF_ARTIFACT in comment.body
    assert comment.omitted_context_diff
