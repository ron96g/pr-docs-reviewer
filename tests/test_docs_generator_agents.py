"""Tests for docs_generator agent structure and configuration."""

from unittest.mock import MagicMock

import pytest

from docs_generator.agent import (
    codebase_scanner,
    doc_planner,
    doc_generator,
    doc_quality_reviewer,
    scan_plan_pipeline,
    page_refinement_loop,
    approve_page,
)


# ---------------------------------------------------------------------------
# Agent structure
# ---------------------------------------------------------------------------

class TestAgentStructure:
    """Verify agent definitions are wired correctly."""

    def test_codebase_scanner_has_correct_tools(self):
        tool_names = [t.__name__ for t in codebase_scanner.tools]
        assert "list_source_files" in tool_names
        assert "read_file_contents" in tool_names
        assert "get_function_signatures" in tool_names

    def test_codebase_scanner_output_key(self):
        assert codebase_scanner.output_key == "codebase_map"

    def test_doc_planner_has_correct_tools(self):
        tool_names = [t.__name__ for t in doc_planner.tools]
        assert "search_docs_by_keyword" in tool_names
        assert "read_doc_file" in tool_names

    def test_doc_planner_output_key(self):
        assert doc_planner.output_key == "doc_plan"

    def test_doc_generator_has_correct_tools(self):
        tool_names = [t.__name__ for t in doc_generator.tools]
        assert "read_file_contents" in tool_names
        assert "get_function_signatures" in tool_names
        assert "read_doc_file" in tool_names

    def test_doc_generator_output_key(self):
        assert doc_generator.output_key == "current_page_draft"

    def test_doc_quality_reviewer_has_approve_tool(self):
        tool_names = [t.__name__ for t in doc_quality_reviewer.tools]
        assert "approve_page" in tool_names

    def test_doc_quality_reviewer_output_key(self):
        assert doc_quality_reviewer.output_key == "page_reviewer_feedback"


class TestScanPlanPipeline:
    """Verify scan_plan_pipeline composition."""

    def test_is_sequential(self):
        assert scan_plan_pipeline.name == "docs_scan_plan"

    def test_sub_agents_order(self):
        names = [a.name for a in scan_plan_pipeline.sub_agents]
        assert names == ["codebase_scanner", "doc_planner"]


class TestPageRefinementLoop:
    """Verify page_refinement_loop composition."""

    def test_is_loop(self):
        assert page_refinement_loop.name == "page_refinement"

    def test_max_iterations(self):
        assert page_refinement_loop.max_iterations == 3

    def test_sub_agents_order(self):
        names = [a.name for a in page_refinement_loop.sub_agents]
        assert names == ["doc_generator", "doc_quality_reviewer"]


# ---------------------------------------------------------------------------
# approve_page escalation tool
# ---------------------------------------------------------------------------

class TestApprovePage:

    def test_sets_escalate_flag(self):
        ctx = MagicMock()
        ctx.actions = MagicMock()
        ctx.actions.escalate = None

        result = approve_page(ctx)

        assert ctx.actions.escalate is True
        assert result["approved"] is True
        assert "approved" in result["message"].lower() or "exit" in result["message"].lower()
