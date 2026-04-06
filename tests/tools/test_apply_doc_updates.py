"""Tests for apply_doc_updates tool — mocks at the backend level via set_backend.

The doc_writer agent now produces full file content per suggestion (one entry
per doc file with ``suggested_content`` containing the complete updated file).
The apply logic simply writes ``suggested_content`` — no text-matching needed.
"""

from __future__ import annotations

import json
from unittest.mock import MagicMock, patch

import pytest

from pr_docs_reviewer.tools.backend import reset_backend, set_backend
from pr_docs_reviewer.tools.apply_doc_updates import (
    apply_doc_updates,
    apply_suggestions,
)


@pytest.fixture(autouse=True)
def _clean_backend():
    reset_backend()
    yield
    reset_backend()


@pytest.fixture
def mock_backend():
    """Create a mock backend with standard write-method stubs."""
    backend = MagicMock()
    backend.get_pr_head_ref.return_value = ("feature/add-retry", "abc123def456")
    backend.create_branch.return_value = None
    backend.write_file.return_value = None
    backend.create_pull_request.return_value = {
        "number": 456,
        "html_url": "https://github.com/acme/widgets/pull/456",
    }
    return backend


ORIGINAL_CONTENT = "# API\n\nThe timeout defaults to 30 seconds.\n\n## Config\n"
SUGGESTED_CONTENT = (
    "# API\n\nThe timeout defaults to 30 seconds. "
    "You can also configure max_retries (default: 3).\n\n## Config\n"
)


@pytest.fixture
def sample_suggestions():
    """Plain suggestion list for apply_suggestions() tests."""
    return [
        {
            "doc_path": "docs/api.md",
            "changes_summary": "Added max_retries parameter documentation.",
            "original_content": ORIGINAL_CONTENT,
            "suggested_content": SUGGESTED_CONTENT,
        },
    ]


@pytest.fixture
def tool_context():
    ctx = MagicMock()
    ctx.state = {
        "doc_suggestions": [
            {
                "doc_path": "docs/api.md",
                "changes_summary": "Added max_retries parameter documentation.",
                "original_content": ORIGINAL_CONTENT,
                "suggested_content": SUGGESTED_CONTENT,
            },
        ],
        "repo": "acme/widgets",
        "pr_number": 42,
        "pr_url": "https://github.com/acme/widgets/pull/42",
    }
    ctx.actions = MagicMock()
    return ctx


# ---------------------------------------------------------------------------
# Tests — basic integration via tool_context
# ---------------------------------------------------------------------------


class TestReadsSuggestionsFromState:

    def test_reads_suggestions_from_state(self, mock_backend, tool_context):
        set_backend(mock_backend)
        result = apply_doc_updates(tool_context)

        assert result["status"] == "success"
        assert result["files_updated"] == ["docs/api.md"]


class TestGetsPrHeadRef:

    def test_gets_pr_head_ref(self, mock_backend, tool_context):
        set_backend(mock_backend)
        apply_doc_updates(tool_context)

        mock_backend.get_pr_head_ref.assert_called_once()


class TestCreatesBranchFromPrHead:

    def test_creates_branch_from_pr_head(self, mock_backend, tool_context):
        set_backend(mock_backend)
        apply_doc_updates(tool_context)

        mock_backend.create_branch.assert_called_once_with(
            "docs/update-for-pr-42", "abc123def456",
        )


class TestWritesFileContent:

    def test_writes_full_suggested_content(self, mock_backend, tool_context):
        set_backend(mock_backend)
        apply_doc_updates(tool_context)

        mock_backend.write_file.assert_called_once()
        call_kwargs = mock_backend.write_file.call_args
        assert call_kwargs[1]["path"] == "docs/api.md"
        assert "max_retries" in call_kwargs[1]["content"]
        assert call_kwargs[1]["branch"] == "docs/update-for-pr-42"
        assert "PR #42" in call_kwargs[1]["message"]

    def test_writes_suggested_content_directly(self, mock_backend, sample_suggestions):
        """The written content should be the suggested_content (with trailing newline)."""
        set_backend(mock_backend)
        apply_suggestions(suggestions=sample_suggestions, pr_number=42)

        written_content = mock_backend.write_file.call_args[1]["content"]
        expected = SUGGESTED_CONTENT.rstrip("\n") + "\n"
        assert written_content == expected


class TestCreatesPrWithCorrectBase:

    def test_creates_pr_with_correct_base(self, mock_backend, tool_context):
        set_backend(mock_backend)
        result = apply_doc_updates(tool_context)

        mock_backend.create_pull_request.assert_called_once()
        call_kwargs = mock_backend.create_pull_request.call_args[1]
        assert call_kwargs["base"] == "feature/add-retry"
        assert call_kwargs["head"] == "docs/update-for-pr-42"
        assert result["doc_pr_url"] == "https://github.com/acme/widgets/pull/456"
        assert result["doc_pr_number"] == 456


class TestSkipsMissingSuggestedContent:

    def test_skips_when_suggested_content_empty(self, mock_backend):
        set_backend(mock_backend)
        suggestions = [
            {
                "doc_path": "docs/api.md",
                "changes_summary": "Something",
                "original_content": ORIGINAL_CONTENT,
                "suggested_content": "",
            },
        ]
        result = apply_suggestions(suggestions=suggestions, pr_number=42)

        assert result["status"] == "success"
        assert result["commit_count"] == 0
        assert len(result["skipped_suggestions"]) == 1
        assert "missing suggested_content" in result["skipped_suggestions"][0]["reason"]

    def test_skips_when_suggested_content_missing(self, mock_backend):
        set_backend(mock_backend)
        suggestions = [
            {
                "doc_path": "docs/api.md",
                "changes_summary": "Something",
                "original_content": ORIGINAL_CONTENT,
                # no suggested_content key
            },
        ]
        result = apply_suggestions(suggestions=suggestions, pr_number=42)

        assert result["status"] == "success"
        assert result["commit_count"] == 0
        assert len(result["skipped_suggestions"]) == 1
        assert "missing suggested_content" in result["skipped_suggestions"][0]["reason"]


class TestHandlesBranchExists:

    def test_branch_exists_appends_timestamp(self, mock_backend, tool_context):
        set_backend(mock_backend)
        # First create_branch fails, second succeeds
        mock_backend.create_branch.side_effect = [
            RuntimeError("Branch 'docs/update-for-pr-42' already exists"),
            None,
        ]

        result = apply_doc_updates(tool_context)

        assert result["status"] == "success"
        assert mock_backend.create_branch.call_count == 2
        # Second call should have a timestamp-appended branch name
        second_call = mock_backend.create_branch.call_args_list[1]
        assert second_call[0][0].startswith("docs/update-for-pr-42-")


class TestHandlesEmptySuggestions:

    def test_empty_suggestions(self, mock_backend, tool_context):
        set_backend(mock_backend)
        tool_context.state["doc_suggestions"] = []

        result = apply_doc_updates(tool_context)

        assert result["status"] == "success"
        assert result["message"] == "No suggestions to apply"
        assert result["files_updated"] == []
        assert result["commit_count"] == 0
        mock_backend.get_pr_head_ref.assert_not_called()

    def test_none_suggestions(self, mock_backend, tool_context):
        set_backend(mock_backend)
        tool_context.state["doc_suggestions"] = None

        result = apply_doc_updates(tool_context)

        assert result["status"] == "success"
        assert result["message"] == "No suggestions to apply"


class TestHandlesBackendError:

    def test_backend_error_on_head_ref(self, mock_backend, tool_context):
        set_backend(mock_backend)
        mock_backend.get_pr_head_ref.side_effect = RuntimeError("API failure")

        result = apply_doc_updates(tool_context)

        assert result["status"] == "error"
        assert "Failed to get PR head ref" in result["error_message"]

    def test_backend_error_on_write_file(self, mock_backend, tool_context):
        set_backend(mock_backend)
        mock_backend.write_file.side_effect = RuntimeError("disk full")

        result = apply_doc_updates(tool_context)

        assert result["status"] == "success"
        assert result["commit_count"] == 0
        assert len(result["skipped_suggestions"]) == 1
        assert "write error" in result["skipped_suggestions"][0]["reason"]


# ===========================================================================
# Tests for apply_suggestions() — standalone function (no ToolContext)
# ===========================================================================


class TestApplySuggestionsStandalone:
    """Test apply_suggestions() called directly with plain Python args."""

    def test_success_with_list(self, mock_backend, sample_suggestions):
        set_backend(mock_backend)
        result = apply_suggestions(
            suggestions=sample_suggestions,
            pr_number=42,
            repo="acme/widgets",
        )

        assert result["status"] == "success"
        assert result["files_updated"] == ["docs/api.md"]
        assert result["doc_pr_url"] == "https://github.com/acme/widgets/pull/456"
        assert result["doc_pr_number"] == 456
        assert result["commit_count"] == 1

    def test_parses_json_string(self, mock_backend, sample_suggestions):
        set_backend(mock_backend)
        result = apply_suggestions(
            suggestions=json.dumps(sample_suggestions),
            pr_number=42,
            repo="acme/widgets",
        )

        assert result["status"] == "success"
        assert result["files_updated"] == ["docs/api.md"]

    def test_strips_markdown_fences_from_json_string(self, mock_backend, sample_suggestions):
        set_backend(mock_backend)
        wrapped = "```json\n" + json.dumps(sample_suggestions) + "\n```"
        result = apply_suggestions(
            suggestions=wrapped,
            pr_number=42,
            repo="acme/widgets",
        )

        assert result["status"] == "success"
        assert result["files_updated"] == ["docs/api.md"]

    def test_empty_list_returns_no_changes(self, mock_backend):
        set_backend(mock_backend)
        result = apply_suggestions(suggestions=[], pr_number=42)

        assert result["status"] == "success"
        assert result["message"] == "No suggestions to apply"
        mock_backend.get_pr_head_ref.assert_not_called()

    def test_none_returns_no_changes(self, mock_backend):
        set_backend(mock_backend)
        result = apply_suggestions(suggestions=None, pr_number=42)

        assert result["status"] == "success"
        assert result["message"] == "No suggestions to apply"

    def test_invalid_json_string_returns_error(self, mock_backend):
        set_backend(mock_backend)
        result = apply_suggestions(
            suggestions="not valid json",
            pr_number=42,
        )

        assert result["status"] == "error"
        assert "could not be parsed" in result["error_message"]

    def test_backend_error_on_head_ref(self, mock_backend, sample_suggestions):
        set_backend(mock_backend)
        mock_backend.get_pr_head_ref.side_effect = RuntimeError("API failure")

        result = apply_suggestions(
            suggestions=sample_suggestions,
            pr_number=42,
        )

        assert result["status"] == "error"
        assert "Failed to get PR head ref" in result["error_message"]

    def test_branch_exists_retries_with_timestamp(self, mock_backend, sample_suggestions):
        set_backend(mock_backend)
        mock_backend.create_branch.side_effect = [
            RuntimeError("Branch already exists"),
            None,
        ]

        result = apply_suggestions(
            suggestions=sample_suggestions,
            pr_number=42,
        )

        assert result["status"] == "success"
        assert mock_backend.create_branch.call_count == 2
        second_call = mock_backend.create_branch.call_args_list[1]
        assert second_call[0][0].startswith("docs/update-for-pr-42-")


class TestApplySuggestionsMatchesToolWrapper:
    """Verify that apply_doc_updates() and apply_suggestions() produce
    identical results for the same inputs."""

    def test_identical_results(self, mock_backend, tool_context, sample_suggestions):
        # Run via tool wrapper
        set_backend(mock_backend)
        tool_result = apply_doc_updates(tool_context)

        # Reset backend state for a clean second run
        reset_backend()
        set_backend(mock_backend)
        mock_backend.reset_mock()
        mock_backend.get_pr_head_ref.return_value = ("feature/add-retry", "abc123def456")
        mock_backend.create_pull_request.return_value = {
            "number": 456,
            "html_url": "https://github.com/acme/widgets/pull/456",
        }

        standalone_result = apply_suggestions(
            suggestions=sample_suggestions,
            pr_number=42,
            repo="acme/widgets",
        )

        assert tool_result["status"] == standalone_result["status"]
        assert tool_result["files_updated"] == standalone_result["files_updated"]
        assert tool_result["commit_count"] == standalone_result["commit_count"]
        assert tool_result["doc_pr_url"] == standalone_result["doc_pr_url"]


class TestMultipleFiles:
    """Test suggestions for multiple doc files in a single call."""

    def test_commits_each_file_separately(self, mock_backend):
        set_backend(mock_backend)
        suggestions = [
            {
                "doc_path": "docs/api.md",
                "changes_summary": "Updated API docs.",
                "original_content": "# API\nOld content.\n",
                "suggested_content": "# API\nNew content.\n",
            },
            {
                "doc_path": "docs/getting-started.md",
                "changes_summary": "Updated getting started.",
                "original_content": "# Getting Started\nOld guide.\n",
                "suggested_content": "# Getting Started\nNew guide.\n",
            },
        ]
        result = apply_suggestions(suggestions=suggestions, pr_number=42)

        assert result["status"] == "success"
        assert result["commit_count"] == 2
        assert result["files_updated"] == ["docs/api.md", "docs/getting-started.md"]
        assert mock_backend.write_file.call_count == 2

    def test_partial_failure_continues(self, mock_backend):
        """If one file fails to write, the other should still succeed."""
        set_backend(mock_backend)
        mock_backend.write_file.side_effect = [
            RuntimeError("disk full"),  # first file fails
            None,                       # second file succeeds
        ]
        suggestions = [
            {
                "doc_path": "docs/api.md",
                "changes_summary": "Updated API.",
                "original_content": "old",
                "suggested_content": "new",
            },
            {
                "doc_path": "docs/guide.md",
                "changes_summary": "Updated guide.",
                "original_content": "old",
                "suggested_content": "new",
            },
        ]
        result = apply_suggestions(suggestions=suggestions, pr_number=42)

        assert result["status"] == "success"
        assert result["commit_count"] == 1
        assert result["files_updated"] == ["docs/guide.md"]
        assert len(result["skipped_suggestions"]) == 1
        assert result["skipped_suggestions"][0]["doc_path"] == "docs/api.md"


# ===========================================================================
# Tests for trailing newline at EOF
# ===========================================================================


class TestTrailingNewlineAtEOF:
    """Ensure written content always ends with exactly one trailing newline."""

    def test_adds_trailing_newline_when_missing(self, mock_backend, sample_suggestions):
        """If suggested_content lacks trailing newline, one is added."""
        set_backend(mock_backend)
        sample_suggestions[0]["suggested_content"] = "# API\n\nNew content."
        result = apply_suggestions(
            suggestions=sample_suggestions,
            pr_number=42,
        )

        assert result["status"] == "success"
        written_content = mock_backend.write_file.call_args[1]["content"]
        assert written_content.endswith("\n"), "Content must end with a trailing newline"
        assert not written_content.endswith("\n\n"), "Content must not end with multiple newlines"

    def test_does_not_double_trailing_newline(self, mock_backend, sample_suggestions):
        """If suggested_content already ends with \\n, don't add another."""
        set_backend(mock_backend)
        sample_suggestions[0]["suggested_content"] = "# API\n\nNew content.\n"
        result = apply_suggestions(
            suggestions=sample_suggestions,
            pr_number=42,
        )

        assert result["status"] == "success"
        written_content = mock_backend.write_file.call_args[1]["content"]
        assert written_content == "# API\n\nNew content.\n"

    def test_collapses_multiple_trailing_newlines(self, mock_backend, sample_suggestions):
        """Multiple trailing newlines should be collapsed to one."""
        set_backend(mock_backend)
        sample_suggestions[0]["suggested_content"] = "# API\n\nNew content.\n\n\n"
        result = apply_suggestions(
            suggestions=sample_suggestions,
            pr_number=42,
        )

        assert result["status"] == "success"
        written_content = mock_backend.write_file.call_args[1]["content"]
        assert written_content == "# API\n\nNew content.\n"


# ===========================================================================
# Tests for PR body with empty/missing changes_summary
# ===========================================================================


class TestPRBodyChangesSummary:
    """Ensure the PR body handles missing or empty changes_summary gracefully."""

    def test_empty_summary_uses_fallback(self, mock_backend):
        set_backend(mock_backend)
        suggestions = [
            {
                "doc_path": "docs/api.md",
                "changes_summary": "",
                "original_content": "old",
                "suggested_content": "new",
            },
        ]
        result = apply_suggestions(suggestions=suggestions, pr_number=42)

        assert result["status"] == "success"
        pr_body = mock_backend.create_pull_request.call_args[1]["body"]
        assert "- **docs/api.md**: updated" in pr_body

    def test_missing_summary_uses_fallback(self, mock_backend):
        set_backend(mock_backend)
        suggestions = [
            {
                "doc_path": "docs/api.md",
                "original_content": "old",
                "suggested_content": "new",
                # no changes_summary key
            },
        ]
        result = apply_suggestions(suggestions=suggestions, pr_number=42)

        assert result["status"] == "success"
        pr_body = mock_backend.create_pull_request.call_args[1]["body"]
        assert "- **docs/api.md**: updated" in pr_body

    def test_whitespace_only_summary_uses_fallback(self, mock_backend):
        set_backend(mock_backend)
        suggestions = [
            {
                "doc_path": "docs/api.md",
                "changes_summary": "   ",
                "original_content": "old",
                "suggested_content": "new",
            },
        ]
        result = apply_suggestions(suggestions=suggestions, pr_number=42)

        assert result["status"] == "success"
        pr_body = mock_backend.create_pull_request.call_args[1]["body"]
        assert "- **docs/api.md**: updated" in pr_body

    def test_present_summary_used_normally(self, mock_backend, sample_suggestions):
        set_backend(mock_backend)
        result = apply_suggestions(
            suggestions=sample_suggestions,
            pr_number=42,
            repo="acme/widgets",
        )

        assert result["status"] == "success"
        pr_body = mock_backend.create_pull_request.call_args[1]["body"]
        assert "- **docs/api.md**: Added max_retries parameter documentation." in pr_body


# ===========================================================================
# Tests for no-read-file requirement
# ===========================================================================


class TestNoBackendReadRequired:
    """The new schema writes suggested_content directly — no read_file calls."""

    def test_does_not_call_read_file(self, mock_backend, sample_suggestions):
        set_backend(mock_backend)
        apply_suggestions(suggestions=sample_suggestions, pr_number=42)

        mock_backend.read_file.assert_not_called()
