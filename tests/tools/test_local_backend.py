"""Tests for LocalBackend — filesystem + git + event payload."""

import json
import os
from pathlib import Path
from unittest.mock import patch, MagicMock

import pytest

from pr_docs_reviewer.tools.local_backend import LocalBackend


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------

@pytest.fixture
def repo_root(tmp_path):
    """Create a minimal fake repo directory structure."""
    # Create some source files
    src = tmp_path / "src"
    src.mkdir()
    (src / "client.py").write_text("class Client:\n    def connect(self):\n        pass\n")
    (src / "utils.py").write_text("def helper():\n    return 42\n")

    # Create a docs directory
    docs = tmp_path / "docs"
    docs.mkdir()
    (docs / "guide.md").write_text(
        "# User Guide\n\nUse the `Client` class to connect.\n\n## API\n\nSee client.py for details.\n"
    )
    (docs / "changelog.md").write_text("# Changelog\n\n## v1.0\n\n- Initial release\n")
    (docs / "readme.txt").write_text("This is a readme.\nSee Client for usage.\n")

    # Create a hidden directory (should be skipped by search)
    hidden = tmp_path / ".git"
    hidden.mkdir()
    (hidden / "config").write_text("some git config mentioning Client\n")

    return tmp_path


@pytest.fixture
def event_payload_file(tmp_path):
    """Create a fake GitHub Actions event payload JSON file."""
    payload = {
        "pull_request": {
            "number": 42,
            "title": "Add retry logic",
            "body": "This PR adds retry support to the Client class.",
            "html_url": "https://github.com/acme/widgets/pull/42",
        }
    }
    path = tmp_path / "event.json"
    path.write_text(json.dumps(payload))
    return str(path)


@pytest.fixture
def local_env(event_payload_file):
    """Common environment variables for local mode tests."""
    return {
        "GITHUB_EVENT_PATH": event_payload_file,
        "GITHUB_REPOSITORY": "acme/widgets",
        "GITHUB_BASE_REF": "main",
        "GITHUB_HEAD_REF": "feature-branch",
    }


# ---------------------------------------------------------------------------
# Construction
# ---------------------------------------------------------------------------

class TestLocalBackendInit:

    def test_explicit_repo_root(self, tmp_path):
        backend = LocalBackend(repo_root=tmp_path)
        assert backend.repo_root == tmp_path

    def test_defaults_to_github_workspace(self, tmp_path):
        with patch.dict(os.environ, {"GITHUB_WORKSPACE": str(tmp_path)}):
            backend = LocalBackend()
        assert backend.repo_root == tmp_path

    def test_defaults_to_cwd_if_no_workspace(self):
        with patch.dict(os.environ, {}, clear=True):
            backend = LocalBackend()
        assert backend.repo_root == Path(os.getcwd())


# ---------------------------------------------------------------------------
# get_pr_metadata
# ---------------------------------------------------------------------------

class TestGetPrMetadata:

    def test_reads_event_payload(self, repo_root, local_env):
        with patch.dict(os.environ, local_env):
            backend = LocalBackend(repo_root=repo_root)
            meta = backend.get_pr_metadata()

        assert meta["number"] == 42
        assert meta["title"] == "Add retry logic"
        assert meta["body"] == "This PR adds retry support to the Client class."
        assert meta["html_url"] == "https://github.com/acme/widgets/pull/42"
        assert meta["repo"] == "acme/widgets"

    def test_missing_event_path_raises(self, repo_root):
        with patch.dict(os.environ, {"GITHUB_EVENT_PATH": ""}, clear=True):
            backend = LocalBackend(repo_root=repo_root)
            with pytest.raises(RuntimeError, match="GITHUB_EVENT_PATH"):
                backend.get_pr_metadata()

    def test_nonexistent_event_file_raises(self, repo_root):
        with patch.dict(os.environ, {"GITHUB_EVENT_PATH": "/no/such/file.json"}, clear=True):
            backend = LocalBackend(repo_root=repo_root)
            with pytest.raises(RuntimeError, match="GITHUB_EVENT_PATH"):
                backend.get_pr_metadata()


# ---------------------------------------------------------------------------
# get_pr_diff
# ---------------------------------------------------------------------------

class TestGetPrDiff:

    def test_calls_git_diff(self, repo_root, local_env):
        with patch.dict(os.environ, local_env):
            backend = LocalBackend(repo_root=repo_root)
            with patch.object(backend, "_git", return_value="fake diff output") as mock_git:
                result = backend.get_pr_diff()

        mock_git.assert_called_once_with("diff", "origin/main...HEAD")
        assert result == "fake diff output"

    def test_missing_base_ref_raises(self, repo_root):
        with patch.dict(os.environ, {"GITHUB_BASE_REF": ""}, clear=True):
            backend = LocalBackend(repo_root=repo_root)
            with pytest.raises(RuntimeError, match="GITHUB_BASE_REF"):
                backend.get_pr_diff()


# ---------------------------------------------------------------------------
# get_pr_files
# ---------------------------------------------------------------------------

class TestGetPrFiles:

    def test_parses_numstat_and_name_status(self, repo_root, local_env):
        numstat_output = "5\t1\tsrc/client.py\n10\t0\tsrc/new.py\n"
        name_status_output = "M\tsrc/client.py\nA\tsrc/new.py\n"

        with patch.dict(os.environ, local_env):
            backend = LocalBackend(repo_root=repo_root)
            with patch.object(backend, "_git") as mock_git:
                mock_git.side_effect = [numstat_output, name_status_output]
                files = backend.get_pr_files()

        assert len(files) == 2
        assert files[0] == {
            "filename": "src/client.py",
            "status": "modified",
            "additions": 5,
            "deletions": 1,
        }
        assert files[1] == {
            "filename": "src/new.py",
            "status": "added",
            "additions": 10,
            "deletions": 0,
        }

    def test_deleted_file_status(self, repo_root, local_env):
        numstat_output = "0\t5\tsrc/old.py\n"
        name_status_output = "D\tsrc/old.py\n"

        with patch.dict(os.environ, local_env):
            backend = LocalBackend(repo_root=repo_root)
            with patch.object(backend, "_git") as mock_git:
                mock_git.side_effect = [numstat_output, name_status_output]
                files = backend.get_pr_files()

        assert files[0]["status"] == "deleted"

    def test_empty_diff(self, repo_root, local_env):
        with patch.dict(os.environ, local_env):
            backend = LocalBackend(repo_root=repo_root)
            with patch.object(backend, "_git", return_value=""):
                files = backend.get_pr_files()

        assert files == []


# ---------------------------------------------------------------------------
# read_file
# ---------------------------------------------------------------------------

class TestReadFile:

    def test_reads_existing_file(self, repo_root):
        backend = LocalBackend(repo_root=repo_root)
        content = backend.read_file("src/client.py")
        assert "class Client" in content

    def test_file_not_found_raises(self, repo_root):
        backend = LocalBackend(repo_root=repo_root)
        with pytest.raises(FileNotFoundError, match="File not found"):
            backend.read_file("src/nonexistent.py")


# ---------------------------------------------------------------------------
# search_code
# ---------------------------------------------------------------------------

class TestSearchCode:

    def test_finds_keyword_in_docs(self, repo_root):
        backend = LocalBackend(repo_root=repo_root)
        results = backend.search_code("Client", path_prefix="docs/")

        assert len(results) >= 1
        paths = [r["path"] for r in results]
        assert any("guide.md" in p for p in paths)

    def test_returns_text_matches(self, repo_root):
        backend = LocalBackend(repo_root=repo_root)
        results = backend.search_code("Client", path_prefix="docs/")

        guide_result = next(r for r in results if "guide.md" in r["path"])
        assert len(guide_result["text_matches"]) >= 1
        assert any("Client" in tm["fragment"] for tm in guide_result["text_matches"])

    def test_case_insensitive_search(self, repo_root):
        backend = LocalBackend(repo_root=repo_root)
        results = backend.search_code("client", path_prefix="docs/")

        # Should still find "Client" in docs
        paths = [r["path"] for r in results]
        assert any("guide.md" in p for p in paths)

    def test_no_results_for_missing_keyword(self, repo_root):
        backend = LocalBackend(repo_root=repo_root)
        results = backend.search_code("zzz_nonexistent_term", path_prefix="docs/")
        assert results == []

    def test_respects_path_prefix(self, repo_root):
        backend = LocalBackend(repo_root=repo_root)
        results = backend.search_code("Client", path_prefix="src/")

        paths = [r["path"] for r in results]
        # Should find in src/ but NOT in docs/
        assert any("src/" in p for p in paths)
        assert not any("docs/" in p for p in paths)

    def test_nonexistent_path_prefix_returns_empty(self, repo_root):
        backend = LocalBackend(repo_root=repo_root)
        results = backend.search_code("Client", path_prefix="no_such_dir/")
        assert results == []

    def test_skips_hidden_directories(self, repo_root):
        backend = LocalBackend(repo_root=repo_root)
        # Search without path_prefix — should NOT find .git/config
        results = backend.search_code("Client")
        paths = [r["path"] for r in results]
        assert not any(".git" in p for p in paths)

    def test_per_page_limit(self, repo_root):
        backend = LocalBackend(repo_root=repo_root)
        results = backend.search_code("Client", per_page=1)
        assert len(results) <= 1

    def test_caps_fragments_per_file(self, repo_root):
        """text_matches per file should be capped at 5."""
        # Create a file with many matching lines
        docs = repo_root / "docs"
        lines = ["line with Client\n"] * 20
        (docs / "many_matches.md").write_text("".join(lines))

        backend = LocalBackend(repo_root=repo_root)
        results = backend.search_code("Client", path_prefix="docs/")

        many = next((r for r in results if "many_matches" in r["path"]), None)
        assert many is not None
        assert len(many["text_matches"]) <= 5

    def test_searches_txt_files(self, repo_root):
        """Non-.md doc files should also be found."""
        backend = LocalBackend(repo_root=repo_root)
        results = backend.search_code("Client", path_prefix="docs/")
        paths = [r["path"] for r in results]
        assert any("readme.txt" in p for p in paths)


# ---------------------------------------------------------------------------
# get_pr_head_ref
# ---------------------------------------------------------------------------

class TestGetPrHeadRef:

    def test_reads_head_from_event_payload(self, repo_root, tmp_path):
        payload = {
            "pull_request": {
                "number": 42,
                "title": "Add retry logic",
                "body": "test",
                "html_url": "https://github.com/acme/widgets/pull/42",
                "head": {
                    "ref": "feature/add-retry",
                    "sha": "abc123def456",
                },
            }
        }
        event_file = tmp_path / "event_head.json"
        event_file.write_text(json.dumps(payload))

        with patch.dict(os.environ, {"GITHUB_EVENT_PATH": str(event_file)}):
            backend = LocalBackend(repo_root=repo_root)
            ref, sha = backend.get_pr_head_ref()

        assert ref == "feature/add-retry"
        assert sha == "abc123def456"


# ---------------------------------------------------------------------------
# create_branch
# ---------------------------------------------------------------------------

class TestCreateBranch:

    def test_runs_git_checkout(self, repo_root):
        backend = LocalBackend(repo_root=repo_root)
        with patch("pr_docs_reviewer.tools.local_backend.subprocess.run") as mock_run:
            mock_run.return_value = MagicMock(returncode=0)
            backend.create_branch("docs/update-for-pr-42", "abc123")

        assert mock_run.call_count == 2
        # First call: delete stale remote branch (best-effort)
        mock_run.assert_any_call(
            ["git", "push", "origin", "--delete", "docs/update-for-pr-42"],
            cwd=repo_root,
            capture_output=True,
        )
        # Second call: create the local branch
        mock_run.assert_any_call(
            ["git", "checkout", "-b", "docs/update-for-pr-42", "abc123"],
            cwd=repo_root,
            check=True,
            capture_output=True,
        )


# ---------------------------------------------------------------------------
# write_file
# ---------------------------------------------------------------------------

class TestWriteFile:

    def test_writes_and_commits(self, repo_root):
        backend = LocalBackend(repo_root=repo_root)
        with patch("pr_docs_reviewer.tools.local_backend.subprocess.run") as mock_run:
            mock_run.return_value = MagicMock(returncode=0)
            backend.write_file(
                path="docs/guide.md",
                content="updated content",
                message="docs: update guide.md",
                branch="docs/update-for-pr-42",
            )

        # File should be written
        assert (repo_root / "docs" / "guide.md").read_text() == "updated content"

        # git add + git commit should be called
        assert mock_run.call_count == 2
        add_call = mock_run.call_args_list[0]
        commit_call = mock_run.call_args_list[1]
        assert add_call[0][0] == ["git", "add", "docs/guide.md"]
        assert commit_call[0][0] == ["git", "commit", "-m", "docs: update guide.md"]

    def test_creates_parent_dirs(self, repo_root):
        backend = LocalBackend(repo_root=repo_root)
        with patch("pr_docs_reviewer.tools.local_backend.subprocess.run") as mock_run:
            mock_run.return_value = MagicMock(returncode=0)
            backend.write_file(
                path="docs/new/nested/file.md",
                content="nested content",
                message="docs: add nested file",
                branch="docs/fix",
            )

        assert (repo_root / "docs" / "new" / "nested" / "file.md").exists()
        assert (repo_root / "docs" / "new" / "nested" / "file.md").read_text() == "nested content"


# ---------------------------------------------------------------------------
# create_pull_request
# ---------------------------------------------------------------------------

class TestCreatePullRequest:

    def test_pushes_and_creates_pr(self, repo_root):
        backend = LocalBackend(repo_root=repo_root)

        push_result = MagicMock(returncode=0)
        pr_result = MagicMock(
            returncode=0,
            stdout="https://github.com/acme/widgets/pull/456\n",
        )

        with patch("pr_docs_reviewer.tools.local_backend.subprocess.run") as mock_run:
            mock_run.side_effect = [push_result, pr_result]
            result = backend.create_pull_request(
                title="docs: update for PR #42",
                body="Automated updates.",
                head="docs/update-for-pr-42",
                base="feature/add-retry",
            )

        assert mock_run.call_count == 2

        # First call: git push
        push_call = mock_run.call_args_list[0]
        assert push_call[0][0] == ["git", "push", "origin", "docs/update-for-pr-42"]

        # Second call: gh pr create
        pr_call = mock_run.call_args_list[1]
        assert pr_call[0][0][:3] == ["gh", "pr", "create"]

        assert result == {
            "number": 456,
            "html_url": "https://github.com/acme/widgets/pull/456",
        }
