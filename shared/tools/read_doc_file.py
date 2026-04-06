"""Tool: read_doc_file — reads a doc file and parses its section structure."""

import re

from google.adk.tools import ToolContext

from .read_file_contents import read_file_contents


def read_doc_file(
    file_path: str,
    tool_context: ToolContext,
    ref: str = "main",
) -> dict:
    """
    Reads the full content of a documentation file and parses its structure.

    The repository is automatically determined from the PR being analyzed
    (stored in session state).

    Args:
        file_path: Path to the doc file (e.g., "docs/agents/llm-agents.md").
        ref: Git ref to read from. Defaults to "main".

    Returns:
        dict with keys:
            - status: "success" or "error"
            - content: str (full file content)
            - sections: list of {heading, level, start_line, end_line}
            - error_message: str (only if status is "error")
    """
    # Fetch the file
    file_result = read_file_contents(file_path, tool_context, ref=ref)
    if file_result["status"] == "error":
        return file_result

    content = file_result["content"]
    sections = _parse_markdown_sections(content)

    return {
        "status": "success",
        "content": content,
        "sections": sections,
    }


def _parse_markdown_sections(content: str) -> list[dict]:
    """
    Parse markdown headings into a section list with line ranges.

    Each section runs from its heading to the line before the next heading
    of equal or higher level (or end of file).
    """
    lines = content.splitlines()
    heading_pattern = re.compile(r"^(#{1,6})\s+(.+)$")

    # First pass: find all headings
    headings = []
    for i, line in enumerate(lines):
        match = heading_pattern.match(line)
        if match:
            level = len(match.group(1))
            heading_text = match.group(2).strip()
            headings.append({
                "heading": heading_text,
                "level": level,
                "start_line": i + 1,  # 1-indexed
            })

    # Second pass: compute end_line for each heading
    sections = []
    for idx, h in enumerate(headings):
        # Find the next heading of equal or higher (lower number) level
        end_line = len(lines)  # default: end of file
        for next_h in headings[idx + 1:]:
            if next_h["level"] <= h["level"]:
                end_line = next_h["start_line"] - 1
                break
        else:
            # No heading of equal/higher level found — check if there's
            # any next heading at all (subsection ends at next heading)
            if idx + 1 < len(headings):
                # This heading has subsections but no sibling — extend to EOF
                end_line = len(lines)
            # else: last heading — already set to EOF

        sections.append({
            "heading": h["heading"],
            "level": h["level"],
            "start_line": h["start_line"],
            "end_line": end_line,
        })

    return sections
