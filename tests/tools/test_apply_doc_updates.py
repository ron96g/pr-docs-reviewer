"""Tests for apply_doc_updates tool — mocks at the backend level via set_backend."""

from __future__ import annotations

import json
from unittest.mock import MagicMock, patch

import pytest

from pr_docs_reviewer.tools.backend import reset_backend, set_backend
from pr_docs_reviewer.tools.apply_doc_updates import (
    apply_doc_updates,
    apply_suggestions,
    _apply_replacement,
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
    backend.read_file.return_value = (
        "# API\n\nThe timeout defaults to 30 seconds.\n\n## Config\n"
    )
    return backend


@pytest.fixture
def sample_suggestions():
    """Plain suggestion list for apply_suggestions() tests."""
    return [
        {
            "doc_path": "docs/api.md",
            "section": "Client Configuration",
            "change_type": "update_text",
            "current_text": "The timeout defaults to 30 seconds.",
            "suggested_text": "The timeout defaults to 30 seconds. You can also configure max_retries (default: 3).",
            "rationale": "New max_retries parameter added.",
        },
    ]


@pytest.fixture
def tool_context():
    ctx = MagicMock()
    ctx.state = {
        "doc_suggestions": [
            {
                "doc_path": "docs/api.md",
                "section": "Client Configuration",
                "change_type": "update_text",
                "current_text": "The timeout defaults to 30 seconds.",
                "suggested_text": "The timeout defaults to 30 seconds. You can also configure max_retries (default: 3).",
                "rationale": "New max_retries parameter added.",
            },
        ],
        "repo": "acme/widgets",
        "pr_number": 42,
        "pr_url": "https://github.com/acme/widgets/pull/42",
    }
    ctx.actions = MagicMock()
    return ctx


# ---------------------------------------------------------------------------
# Tests
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

    def test_writes_file_content(self, mock_backend, tool_context):
        set_backend(mock_backend)
        apply_doc_updates(tool_context)

        mock_backend.write_file.assert_called_once()
        call_kwargs = mock_backend.write_file.call_args
        assert call_kwargs[1]["path"] == "docs/api.md"
        assert "max_retries" in call_kwargs[1]["content"]
        assert call_kwargs[1]["branch"] == "docs/update-for-pr-42"
        assert "PR #42" in call_kwargs[1]["message"]


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


class TestTextReplacementExact:

    def test_exact_match(self):
        content = "# API\n\nThe timeout defaults to 30 seconds.\n\n## Config\n"
        result = _apply_replacement(
            content,
            "The timeout defaults to 30 seconds.",
            "The timeout defaults to 30 seconds. Also max_retries.",
        )
        assert result is not None
        assert "Also max_retries." in result
        assert "# API" in result  # surrounding content preserved


class TestTextReplacementNormalized:

    def test_normalized_whitespace_match(self):
        # Content has extra internal whitespace compared to current_text
        content = "The  timeout   defaults  to  30  seconds."
        result = _apply_replacement(
            content,
            "The timeout defaults to 30 seconds.",
            "New text here.",
        )
        assert result is not None
        assert "New text here." in result


class TestSkipsUnfoundText:

    def test_skips_unfound_text(self, mock_backend, tool_context):
        set_backend(mock_backend)
        # Make the file content not contain the expected text
        mock_backend.read_file.return_value = "# API\n\nSomething completely different.\n"

        result = apply_doc_updates(tool_context)

        assert result["status"] == "success"
        assert result["commit_count"] == 0
        assert len(result["skipped_suggestions"]) == 1
        assert "could not locate" in result["skipped_suggestions"][0]["reason"]


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

    def test_backend_error_on_read_file(self, mock_backend, tool_context):
        set_backend(mock_backend)
        mock_backend.read_file.side_effect = FileNotFoundError("docs/api.md not found")

        result = apply_doc_updates(tool_context)

        assert result["status"] == "success"
        assert result["commit_count"] == 0
        assert len(result["skipped_suggestions"]) == 1
        assert "file not found" in result["skipped_suggestions"][0]["reason"]


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
        mock_backend.read_file.return_value = (
            "# API\n\nThe timeout defaults to 30 seconds.\n\n## Config\n"
        )
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
