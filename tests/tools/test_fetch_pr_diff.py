"""Tests for fetch_pr_diff tool and GitHub client utilities."""

import pytest
from unittest.mock import patch, MagicMock

from pr_docs_reviewer.tools.github_client import parse_pr_url
from pr_docs_reviewer.tools.fetch_pr_diff import fetch_pr_diff, _parse_diff
from pr_docs_reviewer.tools.backend import reset_backend, set_backend


# ---------------------------------------------------------------------------
# A parseable diff for mock responses (matches _FAKE_PR_FILES below)
# ---------------------------------------------------------------------------
_PARSEABLE_DIFF = """\
diff --git a/src/client.py b/src/client.py
index abc1234..def5678 100644
--- a/src/client.py
+++ b/src/client.py
@@ -10,2 +10,5 @@ class Client:
     def connect(self):
-        pass
+        self._retry()
+
+    def _retry(self):
+        pass
"""


def _make_mock_backend(*, diff_text=_PARSEABLE_DIFF, api_files=None):
    """Create a mock backend with standard fake responses."""
    backend = MagicMock()
    backend.get_pr_metadata.return_value = {
        "number": 42,
        "title": "Add retry logic",
        "body": "This PR adds retry support",
        "html_url": "https://github.com/owner/repo/pull/42",
        "repo": "owner/repo",
    }
    backend.get_pr_diff.return_value = diff_text
    backend.get_pr_files.return_value = api_files or [
        {"filename": "src/client.py", "additions": 10, "deletions": 2, "status": "modified"},
    ]
    return backend


@pytest.fixture(autouse=True)
def _reset_backend():
    """Ensure backend singleton is reset between tests."""
    reset_backend()
    yield
    reset_backend()


class TestParsePrUrl:
    """Tests for the PR URL parser."""

    def test_standard_url(self):
        owner, repo, number = parse_pr_url("https://github.com/google/adk-python/pull/42")
        assert owner == "google"
        assert repo == "adk-python"
        assert number == 42

    def test_url_with_files_suffix(self):
        owner, repo, number = parse_pr_url(
            "https://github.com/owner/repo/pull/123/files"
        )
        assert owner == "owner"
        assert repo == "repo"
        assert number == 123

    def test_url_without_scheme(self):
        owner, repo, number = parse_pr_url("github.com/owner/repo/pull/1")
        assert number == 1

    def test_http_url(self):
        owner, repo, number = parse_pr_url("http://github.com/owner/repo/pull/99")
        assert number == 99

    def test_invalid_url(self):
        with pytest.raises(ValueError, match="Invalid GitHub PR URL"):
            parse_pr_url("https://gitlab.com/owner/repo/merge_requests/1")

    def test_not_a_pr_url(self):
        with pytest.raises(ValueError):
            parse_pr_url("https://github.com/owner/repo/issues/5")

    def test_empty_string(self):
        with pytest.raises(ValueError):
            parse_pr_url("")


class TestFetchPrDiff:
    """Tests for the fetch_pr_diff tool (with mocked backend)."""

    def test_invalid_url_returns_error(self, mock_tool_context):
        result = fetch_pr_diff("not-a-url", mock_tool_context)
        assert result["status"] == "error"
        assert "Invalid" in result["error_message"]

    def test_stores_repo_in_state(self, mock_tool_context):
        """Valid URL should populate repo/pr_number/pr_url in state, even on failure."""
        backend = _make_mock_backend()
        backend.get_pr_metadata.side_effect = Exception("Network error")
        set_backend(backend)

        result = fetch_pr_diff(
            "https://github.com/owner/repo/pull/42",
            mock_tool_context,
        )

        assert mock_tool_context.state["repo"] == "owner/repo"
        assert mock_tool_context.state["pr_number"] == 42
        assert mock_tool_context.state["pr_url"] == "https://github.com/owner/repo/pull/42"

    def test_successful_fetch(self, mock_tool_context):
        """Returns structured data on success — no raw diff in result."""
        backend = _make_mock_backend()
        set_backend(backend)

        result = fetch_pr_diff(
            "https://github.com/owner/repo/pull/42",
            mock_tool_context,
        )

        assert result["status"] == "success"
        assert result["pr_number"] == 42
        assert result["pr_title"] == "Add retry logic"
        # files_changed is now a list of dicts, not strings
        assert isinstance(result["files_changed"], list)
        assert isinstance(result["files_changed"][0], dict)
        assert result["files_changed"][0]["path"] == "src/client.py"
        assert result["total_additions"] >= 1
        assert result["total_deletions"] >= 1

    def test_no_raw_diff_in_result(self, mock_tool_context):
        """The raw diff text must NOT appear in the return dict."""
        backend = _make_mock_backend()
        set_backend(backend)

        result = fetch_pr_diff(
            "https://github.com/owner/repo/pull/42",
            mock_tool_context,
        )

        assert "diff" not in result

    def test_files_changed_has_functions_touched(self, mock_tool_context):
        """Parsed diff populates functions_touched for each file."""
        backend = _make_mock_backend()
        set_backend(backend)

        result = fetch_pr_diff(
            "https://github.com/owner/repo/pull/42",
            mock_tool_context,
        )

        fc = result["files_changed"][0]
        assert "functions_touched" in fc
        # The diff contains 'class Client', 'def connect', 'def _retry'
        assert len(fc["functions_touched"]) >= 1
        all_fns = fc["functions_touched"]
        assert any("_retry" in fn for fn in all_fns), f"Expected _retry in {all_fns}"

    def test_files_changed_has_hunk_ranges(self, mock_tool_context):
        """Each file entry includes hunk_ranges with start/end."""
        backend = _make_mock_backend()
        set_backend(backend)

        result = fetch_pr_diff(
            "https://github.com/owner/repo/pull/42",
            mock_tool_context,
        )

        fc = result["files_changed"][0]
        assert "hunk_ranges" in fc
        assert len(fc["hunk_ranges"]) >= 1
        assert "start" in fc["hunk_ranges"][0]
        assert "end" in fc["hunk_ranges"][0]

    def test_writes_changed_files_to_state(self, mock_tool_context):
        """fetch_pr_diff writes a rich changed_files list to state."""
        backend = _make_mock_backend()
        set_backend(backend)

        fetch_pr_diff(
            "https://github.com/owner/repo/pull/42",
            mock_tool_context,
        )

        changed = mock_tool_context.state["changed_files"]
        assert len(changed) == 1
        assert changed[0]["path"] == "src/client.py"
        assert changed[0]["change_type"] == "modified"
        # State entries should have populated functions_touched
        assert len(changed[0]["functions_touched"]) >= 1

    def test_state_has_no_hunk_ranges(self, mock_tool_context):
        """State version of changed_files should NOT include hunk_ranges (too detailed)."""
        backend = _make_mock_backend()
        set_backend(backend)

        fetch_pr_diff(
            "https://github.com/owner/repo/pull/42",
            mock_tool_context,
        )

        for f in mock_tool_context.state["changed_files"]:
            assert "hunk_ranges" not in f

    def test_fallback_on_unparseable_diff(self, mock_tool_context):
        """If PatchSet can't parse the diff, falls back to file list data."""
        backend = _make_mock_backend(
            diff_text="<<<not a real diff>>>",
            api_files=[
                {"filename": "src/client.py", "additions": 10, "deletions": 2, "status": "modified"},
                {"filename": "src/new.py", "additions": 5, "deletions": 0, "status": "added"},
            ],
        )
        set_backend(backend)

        result = fetch_pr_diff(
            "https://github.com/owner/repo/pull/42",
            mock_tool_context,
        )

        assert result["status"] == "success"
        assert len(result["files_changed"]) == 2
        # Fallback entries have empty functions_touched
        assert result["files_changed"][0]["functions_touched"] == []
        assert result["files_changed"][0]["path"] == "src/client.py"
        assert result["files_changed"][1]["path"] == "src/new.py"
        assert result["total_additions"] == 15
        assert result["total_deletions"] == 2

    def test_backend_error(self, mock_tool_context):
        """Returns error dict on backend failure."""
        backend = _make_mock_backend()
        backend.get_pr_metadata.side_effect = Exception("rate limited")
        set_backend(backend)

        result = fetch_pr_diff(
            "https://github.com/owner/repo/pull/42",
            mock_tool_context,
        )

        assert result["status"] == "error"
        assert "rate limited" in result["error_message"]


class TestParseDiff:
    """Tests for the internal _parse_diff helper."""

    def test_empty_diff(self):
        assert _parse_diff("") == []

    def test_whitespace_only(self):
        assert _parse_diff("   \n\n  ") == []

    def test_invalid_diff(self):
        assert _parse_diff("<<<not valid>>>") == []

    def test_single_file_modification(self, sample_diff):
        files = _parse_diff(sample_diff)
        assert len(files) == 1
        assert files[0]["path"] == "src/http/client.py"
        assert files[0]["change_type"] == "modified"
        assert len(files[0]["hunk_ranges"]) >= 1

    def test_multiple_files(self, multi_file_diff):
        files = _parse_diff(multi_file_diff)
        assert len(files) == 3
        paths = [f["path"] for f in files]
        assert len(paths) == len(set(paths)), "Duplicate paths detected"

    def test_detects_added_file(self, multi_file_diff):
        files = _parse_diff(multi_file_diff)
        added = [f for f in files if f["change_type"] == "added"]
        assert len(added) == 1
        assert "pool.py" in added[0]["path"]

    def test_detects_deleted_file(self, multi_file_diff):
        files = _parse_diff(multi_file_diff)
        deleted = [f for f in files if f["change_type"] == "deleted"]
        assert len(deleted) == 1
        assert "legacy.py" in deleted[0]["path"]

    def test_extracts_function_names(self, sample_diff):
        files = _parse_diff(sample_diff)
        fns = files[0]["functions_touched"]
        assert any("__init__" in fn for fn in fns) or any(
            "request" in fn for fn in fns
        ), f"Expected function names in {fns}"

    def test_extracts_class_definitions(self):
        diff = """\
diff --git a/src/models.py b/src/models.py
index abc1234..def5678 100644
--- a/src/models.py
+++ b/src/models.py
@@ -0,0 +1,3 @@
+class UserProfile:
+    def __init__(self):
+        pass
"""
        files = _parse_diff(diff)
        fns = files[0]["functions_touched"]
        assert any("UserProfile" in fn or "__init__" in fn for fn in fns)

    def test_inline_added_file(self):
        diff = """\
diff --git a/src/new.py b/src/new.py
new file mode 100644
index 0000000..1234567
--- /dev/null
+++ b/src/new.py
@@ -0,0 +1,2 @@
+def hello():
+    pass
"""
        files = _parse_diff(diff)
        assert len(files) == 1
        assert files[0]["change_type"] == "added"
        assert files[0]["path"] == "src/new.py"

    def test_inline_deleted_file(self):
        diff = """\
diff --git a/src/old.py b/src/old.py
deleted file mode 100644
index 1234567..0000000
--- a/src/old.py
+++ /dev/null
@@ -1,2 +0,0 @@
-def goodbye():
-    pass
"""
        files = _parse_diff(diff)
        assert len(files) == 1
        assert files[0]["change_type"] == "deleted"

    def test_per_file_additions_deletions(self, sample_diff):
        files = _parse_diff(sample_diff)
        f = files[0]
        assert f["additions"] >= 1
        assert f["deletions"] >= 1
