import subprocess

import pytest
from pathlib import Path

from click.testing import CliRunner

from bktools import diffcomment


@pytest.fixture(autouse=True)
def clean_ci_env(monkeypatch: pytest.MonkeyPatch) -> None:
    for name in (
        "BUILDKITE_PULL_REQUEST",
        "BUILDKITE_PULL_REQUEST_REPO",
        "BUILDKITE_REPO",
        "BUILDKITE_BUILD_URL",
        "BUILDKITE_COMMIT",
        "GITHUB_EVENT_NAME",
        "GITHUB_EVENT_PATH",
        "GITHUB_REPOSITORY",
        "ACTIONS_ID_TOKEN_REQUEST_TOKEN",
        "ACTIONS_ID_TOKEN_REQUEST_URL",
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
    monkeypatch.setattr(
        diffcomment, "request_github_proxy_token", lambda ci_system="buildkite": "token"
    )
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
    monkeypatch.setattr(
        diffcomment, "request_github_proxy_token", lambda ci_system="buildkite": "token"
    )
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


def test_main_generates_input_checkout_from_repo(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    generated_dir = tmp_path / "output"
    generated_dir.mkdir()
    calls = []
    monkeypatch.setenv("BUILDKITE_PULL_REQUEST", "42")
    monkeypatch.setattr(
        diffcomment,
        "run_manifest_builder_on_checkout",
        lambda repo, *, create_commit: (
            calls.append((repo, create_commit)) or generated_dir
        ),
    )
    monkeypatch.setattr(
        diffcomment,
        "run_manifest_builder_diff",
        lambda input_dir: (
            0,
            diffcomment.ManifestDiff(stat=f"{input_dir.name}.yaml | 1 +\n", diff=""),
        ),
    )

    result = CliRunner().invoke(
        diffcomment.main,
        [
            "--dump",
            "--repo",
            "https://github.com/nresare/manifests.git",
        ],
    )

    assert result.exit_code == 0
    assert calls == [("https://github.com/nresare/manifests.git", False)]
    assert "output.yaml | 1 +" in result.stdout


def test_main_posts_comment_for_github_actions_pull_request(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    posted = []
    input_dir = tmp_path / "output"
    event_path = tmp_path / "event.json"
    input_dir.mkdir()
    event_path.write_text('{"number": 42, "repository": {"full_name": "ignored/repo"}}')
    monkeypatch.setenv("GITHUB_EVENT_NAME", "pull_request")
    monkeypatch.setenv("GITHUB_EVENT_PATH", str(event_path))
    monkeypatch.setenv("GITHUB_REPOSITORY", "nresare/berries-config")
    monkeypatch.setattr(
        diffcomment,
        "run_manifest_builder_diff",
        lambda input_dir: (
            0,
            diffcomment.ManifestDiff(stat="berries.yaml | 1 +\n", diff="diff"),
        ),
    )
    monkeypatch.setattr(
        diffcomment,
        "request_github_proxy_token",
        lambda ci_system="buildkite": f"{ci_system}-token",
    )
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
            "--ci-system=github",
            "--input",
            str(input_dir),
        ],
    )

    assert result.exit_code == 0
    assert posted == [
        (
            "github-token",
            "nresare",
            "berries-config",
            "42",
            "```\n"
            "berries.yaml | 1 +\n"
            "```\n\n"
            "```diff\n"
            "diff\n"
            "```\n\n"
            f"manifest-builder version: `{diffcomment.MANIFEST_BUILDER_VERSION}`",
        )
    ]


def test_main_skips_github_actions_non_pull_request(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    event_path = tmp_path / "event.json"
    event_path.write_text('{"repository": {"full_name": "nresare/berries-config"}}')
    monkeypatch.setenv("GITHUB_EVENT_NAME", "push")
    monkeypatch.setenv("GITHUB_EVENT_PATH", str(event_path))

    result = CliRunner().invoke(diffcomment.main, ["--ci-system=github"])

    assert result.exit_code == 0
    assert "skipping manifest diff comment" in result.stderr


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
    monkeypatch.setattr(
        diffcomment, "request_github_proxy_token", lambda ci_system="buildkite": "token"
    )
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
        lambda repo_root, ci_system="buildkite": artifacts.append(
            (repo_root, ci_system)
        ),
    )

    result = CliRunner().invoke(
        diffcomment.main,
        ["--input", str(input_dir)],
    )

    artifact_path = tmp_path / diffcomment.FULL_DIFF_ARTIFACT
    assert result.exit_code == 0
    assert artifact_path.read_text().startswith("berries.yaml | 1 +\n\n")
    assert artifacts == [(tmp_path, "buildkite")]
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


def test_main_requires_input_or_repo_for_pull_request(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("BUILDKITE_PULL_REQUEST", "42")

    result = CliRunner().invoke(diffcomment.main)

    assert result.exit_code == 2
    assert "requires --input or --repo" in result.output


def test_main_rejects_input_and_repo_together(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    input_dir = tmp_path / "output"
    input_dir.mkdir()
    monkeypatch.setenv("BUILDKITE_PULL_REQUEST", "42")

    result = CliRunner().invoke(
        diffcomment.main,
        [
            "--input",
            str(input_dir),
            "--repo",
            "https://github.com/nresare/manifests.git",
        ],
    )

    assert result.exit_code == 2
    assert "only one of --input or --repo" in result.output


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
    subprocess.run(
        ["git", "commit", "--no-gpg-sign", "-m", "Initial"],
        cwd=input_dir,
        check=True,
    )

    (input_dir / "old.yaml").unlink()
    (input_dir / "new.yaml").write_text("name: original\n")

    _, manifest_diff = diffcomment.run_manifest_builder_diff(input_dir)

    assert "old.yaml => new.yaml" in manifest_diff.stat
    assert "rename from old.yaml" in manifest_diff.diff
    assert "rename to new.yaml" in manifest_diff.diff


def test_run_manifest_builder_diff_summarizes_repeated_metadata_and_filters_noise(
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
    (input_dir / "deployment.yaml").write_text(
        "apiVersion: apps/v1\n"
        "kind: Deployment\n"
        "metadata:\n"
        "  name: app\n"
        "  labels:\n"
        "    app.kubernetes.io/version: v1.8.0\n"
        "  annotations:\n"
        "    noa.re/deploy-id: old-deploy\n"
        "spec:\n"
        "  template:\n"
        "    spec:\n"
        "      containers:\n"
        "      - name: app\n"
        "        image: example/app:v1.8.0\n"
    )
    (input_dir / "service.yaml").write_text(
        "apiVersion: v1\n"
        "kind: Service\n"
        "metadata:\n"
        "  name: app\n"
        "  labels:\n"
        "    app.kubernetes.io/version: v1.8.0\n"
        "  annotations:\n"
        "    noa.re/deploy-id: old-deploy\n"
        "spec:\n"
        "  ports:\n"
        "  - port: 80\n"
    )
    subprocess.run(["git", "add", "."], cwd=input_dir, check=True)
    subprocess.run(
        ["git", "commit", "--no-gpg-sign", "-m", "Initial"],
        cwd=input_dir,
        check=True,
    )

    (input_dir / "deployment.yaml").write_text(
        "apiVersion: apps/v1\n"
        "kind: Deployment\n"
        "metadata:\n"
        "  name: app\n"
        "  labels:\n"
        "    app.kubernetes.io/version: v1.8.1\n"
        "  annotations:\n"
        "    noa.re/deploy-id: new-deploy\n"
        "spec:\n"
        "  template:\n"
        "    spec:\n"
        "      containers:\n"
        "      - name: app\n"
        "        image: example/app:v1.8.1\n"
    )
    (input_dir / "service.yaml").write_text(
        "apiVersion: v1\n"
        "kind: Service\n"
        "metadata:\n"
        "  name: app\n"
        "  labels:\n"
        "    app.kubernetes.io/version: v1.8.1\n"
        "  annotations:\n"
        "    noa.re/deploy-id: new-deploy\n"
        "spec:\n"
        "  ports:\n"
        "  - port: 80\n"
    )

    _, manifest_diff = diffcomment.run_manifest_builder_diff(input_dir)

    assert (
        "- Label `app.kubernetes.io/version` changed from `v1.8.0` to `v1.8.1` "
        "on 2 manifests."
    ) in manifest_diff.summary
    assert "noa.re/deploy-id" in manifest_diff.diff
    assert "noa.re/deploy-id" not in manifest_diff.summary
    assert manifest_diff.filtered_diff is not None
    assert "noa.re/deploy-id" not in manifest_diff.filtered_diff
    assert "app.kubernetes.io/version" not in manifest_diff.filtered_diff
    assert "image: example/app:v1.8.1" in manifest_diff.filtered_diff


def test_filter_metadata_hunks_handles_metadata_context_headers() -> None:
    raw_diff = (
        "diff --git a/deployment.yaml b/deployment.yaml\n"
        "index 1234567..89abcde 100644\n"
        "--- a/deployment.yaml\n"
        "+++ b/deployment.yaml\n"
        "@@ -8,7 +8,9 @@ metadata:\n"
        "     control-plane: envoy-gateway\n"
        "     app.kubernetes.io/name: gateway-helm\n"
        "     app.kubernetes.io/instance: envoy-gateway\n"
        "-    app.kubernetes.io/version: v1.8.0\n"
        "+    app.kubernetes.io/version: v1.8.1\n"
        "+  annotations:\n"
        "+    noa.re/deploy-id: generated\n"
        " spec:\n"
        "   replicas: 1\n"
    )

    filtered_diff = diffcomment.filter_metadata_hunks(
        raw_diff,
        {
            ("metadata", "labels", "app.kubernetes.io/version"),
            ("metadata", "annotations", "noa.re/deploy-id"),
        },
    )

    assert filtered_diff == ""


def test_filter_metadata_hunks_treats_null_annotations_as_absent() -> None:
    raw_diff = (
        "diff --git a/apiservice.yaml b/apiservice.yaml\n"
        "index 1234567..89abcde 100644\n"
        "--- a/apiservice.yaml\n"
        "+++ b/apiservice.yaml\n"
        "@@ -6,8 +6,9 @@ metadata:\n"
        "   labels:\n"
        "     app.kubernetes.io/name: metrics-server\n"
        "     app.kubernetes.io/instance: metrics-server\n"
        "-    app.kubernetes.io/version: 0.8.0\n"
        "-  annotations: null\n"
        "+    app.kubernetes.io/version: 0.8.1\n"
        "+  annotations:\n"
        "+    noa.re/deploy-id: generated\n"
        " spec:\n"
        "   group: metrics.k8s.io\n"
    )

    filtered_diff = diffcomment.filter_metadata_hunks(
        raw_diff,
        {
            ("metadata", "labels", "app.kubernetes.io/version"),
            ("metadata", "annotations", "noa.re/deploy-id"),
        },
    )

    assert filtered_diff == ""


def test_build_comment_body_includes_metadata_summary_and_filtered_diff() -> None:
    comment = diffcomment.build_comment_body(
        "12",
        0,
        diffcomment.ManifestDiff(
            stat="deployment.yaml | 4 ++--\n",
            diff="diff --git a/deployment.yaml b/deployment.yaml\n-noise\n+noise",
            summary="- Label `app.kubernetes.io/version` changed from `v1` to `v2` on 2 manifests.",
            filtered_diff="diff --git a/deployment.yaml b/deployment.yaml\n-old\n+new",
        ),
    )

    assert "Metadata changes:" in comment.body
    assert "changed from `v1` to `v2` on 2 manifests" in comment.body
    assert (
        "Repeated metadata-only changes have been summarized or omitted" in comment.body
    )
    assert "-noise" not in comment.body
    assert "+new" in comment.body
    assert comment.omitted_context_diff


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


def test_request_github_actions_proxy_token(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    requested = []
    monkeypatch.setenv(
        "ACTIONS_ID_TOKEN_REQUEST_URL", "https://actions.example/id-token?api=v1"
    )
    monkeypatch.setenv("ACTIONS_ID_TOKEN_REQUEST_TOKEN", "request-token")
    monkeypatch.setenv("BKTOOLS_GITHUB_PROXY_AUDIENCE", "proxy.example")

    class FakeResponse:
        def read(self) -> bytes:
            return b'{"value": "oidc-token"}'

    class FakeUrlopen:
        def __init__(self, request: object, timeout: int) -> None:
            requested.append((request, timeout))

        def __enter__(self) -> FakeResponse:
            return FakeResponse()

        def __exit__(self, *args: object) -> None:
            return None

    def fake_urlopen(request: object, timeout: int) -> FakeUrlopen:
        return FakeUrlopen(request, timeout)

    monkeypatch.setattr(diffcomment.urllib.request, "urlopen", fake_urlopen)

    assert diffcomment.request_github_proxy_token("github") == "oidc-token"

    request, timeout = requested[0]
    assert timeout == 30
    assert (
        request.full_url
        == "https://actions.example/id-token?api=v1&audience=proxy.example"
    )
    assert request.headers["Authorization"] == "Bearer request-token"


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


def test_build_comment_body_can_use_github_actions_artifact_wording() -> None:
    comment = diffcomment.build_comment_body(
        "12",
        0,
        diffcomment.ManifestDiff(
            stat="file.yaml | 1 +\n",
            diff="x" * diffcomment.MAX_COMMENT_CHARS,
        ),
        full_diff_reference=diffcomment.full_diff_reference("github"),
    )

    assert "GitHub Actions artifact" in comment.body
    assert "Buildkite artifact" not in comment.body
