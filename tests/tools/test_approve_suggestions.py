"""Tests for the approve_suggestions escalation tool."""

import pytest

from pr_docs_reviewer.agent import approve_suggestions


class TestApproveSuggestions:
    """Tests for the escalation tool used by quality_reviewer."""

    def test_sets_escalate(self, mock_tool_context):
        result = approve_suggestions(mock_tool_context)
        assert result["approved"] is True
        assert mock_tool_context.actions.escalate is True

    def test_returns_confirmation_message(self, mock_tool_context):
        result = approve_suggestions(mock_tool_context)
        assert "approved" in result["message"].lower() or "approved" in str(result)
