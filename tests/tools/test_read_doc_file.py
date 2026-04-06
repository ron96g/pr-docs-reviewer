"""Tests for read_doc_file tool — markdown section parsing."""

import pytest
from unittest.mock import patch

from pr_docs_reviewer.tools.read_doc_file import read_doc_file, _parse_markdown_sections


class TestParseMarkdownSections:
    """Tests for the markdown section parser."""

    def test_basic_headings(self):
        content = """\
# Title

Some intro text.

## Section One

Content here.

## Section Two

More content.
"""
        sections = _parse_markdown_sections(content)
        headings = [s["heading"] for s in sections]
        assert headings == ["Title", "Section One", "Section Two"]

    def test_heading_levels(self):
        content = """\
# H1
## H2
### H3
"""
        sections = _parse_markdown_sections(content)
        levels = [s["level"] for s in sections]
        assert levels == [1, 2, 3]

    def test_section_line_ranges(self):
        content = """\
# Title

Intro.

## First

First content.
More first content.

## Second

Second content.
"""
        sections = _parse_markdown_sections(content)
        # "First" starts at its heading and ends before "Second"
        first = next(s for s in sections if s["heading"] == "First")
        second = next(s for s in sections if s["heading"] == "Second")
        assert first["end_line"] < second["start_line"]

    def test_nested_headings(self):
        content = """\
# API Reference

## Client

Main client.

### Configuration

Config options.

### Methods

Method list.

## Server

Server docs.
"""
        sections = _parse_markdown_sections(content)

        # "Client" (h2) should end before "Server" (h2), not at "Configuration" (h3)
        client = next(s for s in sections if s["heading"] == "Client")
        server = next(s for s in sections if s["heading"] == "Server")
        config = next(s for s in sections if s["heading"] == "Configuration")

        assert config["start_line"] > client["start_line"]
        assert config["start_line"] < server["start_line"]
        assert client["end_line"] < server["start_line"]

    def test_no_headings(self):
        content = "Just some plain text\nwith no headings.\n"
        sections = _parse_markdown_sections(content)
        assert sections == []

    def test_empty_content(self):
        sections = _parse_markdown_sections("")
        assert sections == []

    def test_heading_only(self):
        content = "# Solo Heading"
        sections = _parse_markdown_sections(content)
        assert len(sections) == 1
        assert sections[0]["heading"] == "Solo Heading"
        assert sections[0]["start_line"] == 1
        assert sections[0]["end_line"] == 1

    def test_h6_heading(self):
        content = "###### Deep Heading\n\nContent.\n"
        sections = _parse_markdown_sections(content)
        assert sections[0]["level"] == 6

    def test_not_a_heading(self):
        """Lines with # that aren't headings should be ignored."""
        content = """\
# Real Heading

This has a # in it but it's not a heading.
And `## code` is not a heading either.
"""
        sections = _parse_markdown_sections(content)
        assert len(sections) == 1
        assert sections[0]["heading"] == "Real Heading"

    def test_fixture_file(self, sample_doc):
        """Test against the full sample doc fixture."""
        sections = _parse_markdown_sections(sample_doc)
        headings = [s["heading"] for s in sections]

        assert "HTTP Client" in headings
        assert "Installation" in headings
        assert "Configuration" in headings
        assert "Usage" in headings
        assert "Connection Pool" in headings
        assert "Pool Configuration" in headings
        assert "Error Handling" in headings
        assert "Changelog" in headings

        # Pool Configuration (h3) should be nested inside Connection Pool (h2)
        pool = next(s for s in sections if s["heading"] == "Connection Pool")
        pool_config = next(s for s in sections if s["heading"] == "Pool Configuration")
        assert pool_config["start_line"] > pool["start_line"]
        assert pool_config["level"] == 3
        assert pool["level"] == 2


class TestReadDocFileIntegration:
    """Integration tests that mock the file fetching."""

    def test_returns_content_and_sections(self, sample_doc, mock_tool_context):
        mock_tool_context.state["repo"] = "owner/repo"

        with patch(
            "shared.tools.read_doc_file.read_file_contents"
        ) as mock_read:
            mock_read.return_value = {
                "status": "success",
                "content": sample_doc,
                "size_bytes": len(sample_doc),
            }

            result = read_doc_file("docs/http-client.md", mock_tool_context)

            assert result["status"] == "success"
            assert result["content"] == sample_doc
            assert len(result["sections"]) > 0

    def test_propagates_file_error(self, mock_tool_context):
        mock_tool_context.state["repo"] = "owner/repo"

        with patch(
            "shared.tools.read_doc_file.read_file_contents"
        ) as mock_read:
            mock_read.return_value = {
                "status": "error",
                "error_message": "Not found",
            }

            result = read_doc_file("docs/missing.md", mock_tool_context)

            assert result["status"] == "error"
