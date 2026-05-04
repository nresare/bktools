import subprocess

import pytest
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


def test_main_posts_comment_for_pull_request(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    posted = []
    input_dir = tmp_path / "output"
    input_dir.mkdir()
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
        ["--input", str(input_dir)],
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
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    posted = []
    input_dir = tmp_path / "output"
    input_dir.mkdir()
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
            "--input",
            str(input_dir),
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
    input_dir = tmp_path / "output"
    input_dir.mkdir()
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
        ["--input", str(input_dir)],
    )

    artifact_path = tmp_path / diffcomment.FULL_DIFF_ARTIFACT
    assert result.exit_code == 0
    assert artifact_path.read_text().startswith("berries.yaml | 1 +\n\n")
    assert artifacts == [tmp_path]
    assert diffcomment.FULL_DIFF_ARTIFACT in posted[0][4]
    assert "```diff" not in posted[0][4]
    assert posted[0][4].startswith("```\nberries.yaml | 1 +\n```")


def test_main_dump_works_outside_pull_request(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    input_dir = tmp_path / "output"
    input_dir.mkdir()
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
            "--input",
            str(input_dir),
        ],
    )

    assert result.exit_code == 0
    assert "Pull request:" not in result.stdout
    assert "```diff\ndiff\n```" in result.stdout


def test_main_requires_input_for_pull_request(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("BUILDKITE_PULL_REQUEST", "42")

    result = CliRunner().invoke(diffcomment.main)

    assert result.exit_code == 2
    assert "requires --input" in result.output


def test_run_manifest_builder_diff_captures_git_diff(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    input_dir = tmp_path / "output"
    calls = []

    def fake_add_all(output: Path) -> None:
        calls.append(("add_all", output))

    def fake_diff_stat(output: Path) -> str:
        calls.append(("diff_stat", output))
        return "diff stat\n"

    def fake_diff(output: Path) -> str:
        calls.append(("diff", output))
        return "diff output\n"

    monkeypatch.setattr(diffcomment, "git_add_all", fake_add_all)
    monkeypatch.setattr(diffcomment, "git_diff_stat", fake_diff_stat)
    monkeypatch.setattr(diffcomment, "git_diff", fake_diff)

    assert diffcomment.run_manifest_builder_diff(input_dir) == (
        0,
        diffcomment.ManifestDiff(stat="diff stat\n", diff="diff output\n"),
    )
    assert calls == [
        ("add_all", input_dir),
        ("diff_stat", input_dir),
        ("diff", input_dir),
    ]


def test_run_manifest_builder_diff_includes_added_file_after_move(
    tmp_path: Path,
) -> None:
    input_dir = tmp_path / "output"
    input_dir.mkdir()

    subprocess.run(["git", "init"], cwd=input_dir, check=True)
    subprocess.run(
        ["git", "config", "user.email", "test@example.com"], cwd=input_dir, check=True
    )
    subprocess.run(
        ["git", "config", "user.name", "Test User"], cwd=input_dir, check=True
    )
    (input_dir / "old.yaml").write_text("name: original\n")
    subprocess.run(["git", "add", "."], cwd=input_dir, check=True)
    subprocess.run(["git", "commit", "-m", "Initial"], cwd=input_dir, check=True)

    (input_dir / "old.yaml").unlink()
    (input_dir / "new.yaml").write_text("name: original\n")

    _, manifest_diff = diffcomment.run_manifest_builder_diff(input_dir)

    assert "old.yaml => new.yaml" in manifest_diff.stat
    assert "rename from old.yaml" in manifest_diff.diff
    assert "rename to new.yaml" in manifest_diff.diff


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


def test_build_comment_body_preserves_stat_leading_alignment() -> None:
    comment = diffcomment.build_comment_body(
        "12",
        0,
        diffcomment.ManifestDiff(
            stat=" file.yaml | 1 +\n 1 file changed, 1 insertion(+)\n",
            diff="diff",
        ),
    )

    assert comment.body.startswith(
        "```\n file.yaml | 1 +\n 1 file changed, 1 insertion(+)\n```"
    )


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
