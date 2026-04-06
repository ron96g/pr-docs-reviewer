"""Tests for list_files() method on LocalBackend and GitHubAPIBackend."""

from pathlib import Path
from unittest.mock import patch, MagicMock

import pytest

from shared.tools.local_backend import LocalBackend
from shared.tools.github_api_backend import GitHubAPIBackend


# ---------------------------------------------------------------------------
# LocalBackend.list_files
# ---------------------------------------------------------------------------

class TestLocalBackendListFiles:

    @pytest.fixture
    def repo_root(self, tmp_path):
        """Create a minimal fake repo with various file types."""
        (tmp_path / "src").mkdir()
        (tmp_path / "src" / "main.py").write_text("print('hello')\n")
        (tmp_path / "src" / "utils.py").write_text("def helper(): pass\n")
        (tmp_path / "src" / "styles.css").write_text("body {}\n")

        (tmp_path / "docs").mkdir()
        (tmp_path / "docs" / "guide.md").write_text("# Guide\n")

        (tmp_path / "tests").mkdir()
        (tmp_path / "tests" / "test_main.py").write_text("def test(): pass\n")

        # Hidden dir — should be skipped
        (tmp_path / ".git").mkdir()
        (tmp_path / ".git" / "config").write_text("git config\n")

        # Nested hidden dir
        (tmp_path / "src" / ".hidden").mkdir()
        (tmp_path / "src" / ".hidden" / "secret.py").write_text("x = 1\n")

        return tmp_path

    def test_lists_all_matching_extensions(self, repo_root):
        backend = LocalBackend(repo_root=repo_root)
        files = backend.list_files(extensions=[".py", ".md"])

        assert "src/main.py" in files
        assert "src/utils.py" in files
        assert "docs/guide.md" in files
        assert "tests/test_main.py" in files
        # .css should NOT be included
        assert "src/styles.css" not in files

    def test_respects_path_prefix(self, repo_root):
        backend = LocalBackend(repo_root=repo_root)
        files = backend.list_files(path_prefix="src/", extensions=[".py"])

        assert "src/main.py" in files
        assert "src/utils.py" in files
        assert "tests/test_main.py" not in files

    def test_skips_hidden_dirs(self, repo_root):
        backend = LocalBackend(repo_root=repo_root)
        files = backend.list_files(extensions=[".py"])

        for f in files:
            assert ".git" not in f
            assert ".hidden" not in f

    def test_returns_sorted_paths(self, repo_root):
        backend = LocalBackend(repo_root=repo_root)
        files = backend.list_files(extensions=[".py"])
        assert files == sorted(files)

    def test_empty_extensions_returns_all(self, repo_root):
        """Empty list is falsy in Python, so treated same as None (no filter)."""
        backend = LocalBackend(repo_root=repo_root)
        files = backend.list_files(extensions=[])

        # Should include all non-hidden files
        assert "src/main.py" in files
        assert "src/styles.css" in files
        assert "docs/guide.md" in files

    def test_none_extensions_returns_all(self, repo_root):
        backend = LocalBackend(repo_root=repo_root)
        files = backend.list_files(extensions=None)

        # Should include .py, .md, .css — anything not hidden
        assert "src/main.py" in files
        assert "src/styles.css" in files
        assert "docs/guide.md" in files

    def test_nonexistent_prefix_returns_empty(self, repo_root):
        backend = LocalBackend(repo_root=repo_root)
        files = backend.list_files(path_prefix="no_such_dir/", extensions=[".py"])
        assert files == []


# ---------------------------------------------------------------------------
# GitHubAPIBackend.list_files
# ---------------------------------------------------------------------------

class TestGitHubAPIBackendListFiles:

    @pytest.fixture
    def backend(self):
        b = GitHubAPIBackend()
        b.configure(owner="acme", repo="widgets", pr_number=1)
        return b

    def _mock_response(self, json_data):
        """Create a mock httpx.Response with a .json() method."""
        resp = MagicMock()
        resp.json.return_value = json_data
        return resp

    def test_parses_tree_response(self, backend):
        tree_data = {
            "tree": [
                {"path": "src/main.py", "type": "blob"},
                {"path": "src/utils.py", "type": "blob"},
                {"path": "docs/guide.md", "type": "blob"},
                {"path": "src/styles.css", "type": "blob"},
                {"path": "src", "type": "tree"},  # directory — should be skipped
            ]
        }

        with patch("shared.tools.github_api_backend.github_get",
                    return_value=self._mock_response(tree_data)):
            files = backend.list_files(extensions=[".py"])

        assert "src/main.py" in files
        assert "src/utils.py" in files
        assert "docs/guide.md" not in files
        assert "src/styles.css" not in files

    def test_respects_path_prefix(self, backend):
        tree_data = {
            "tree": [
                {"path": "src/main.py", "type": "blob"},
                {"path": "tests/test_main.py", "type": "blob"},
            ]
        }

        with patch("shared.tools.github_api_backend.github_get",
                    return_value=self._mock_response(tree_data)):
            files = backend.list_files(path_prefix="src/", extensions=[".py"])

        assert files == ["src/main.py"]

    def test_returns_sorted(self, backend):
        tree_data = {
            "tree": [
                {"path": "z.py", "type": "blob"},
                {"path": "a.py", "type": "blob"},
                {"path": "m.py", "type": "blob"},
            ]
        }

        with patch("shared.tools.github_api_backend.github_get",
                    return_value=self._mock_response(tree_data)):
            files = backend.list_files(extensions=[".py"])

        assert files == ["a.py", "m.py", "z.py"]

    def test_none_extensions_returns_all_blobs(self, backend):
        tree_data = {
            "tree": [
                {"path": "file.py", "type": "blob"},
                {"path": "file.js", "type": "blob"},
                {"path": "dir", "type": "tree"},
            ]
        }

        with patch("shared.tools.github_api_backend.github_get",
                    return_value=self._mock_response(tree_data)):
            files = backend.list_files(extensions=None)

        assert "file.py" in files
        assert "file.js" in files
        assert "dir" not in files
