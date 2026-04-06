"""Tool: fetch_pr_diff — fetches PR metadata and diff, parses inline.

Returns a compact structured summary to the LLM.  The raw diff is never
returned — it is parsed via ``unidiff.PatchSet`` immediately after
fetching, and only per-file summaries (change type, functions touched,
hunk line-ranges) are included in the result.
"""

import os
import re

from google.adk.tools import ToolContext
from unidiff import PatchSet

from shared.tools.backend import get_backend
from shared.tools.github_client import parse_pr_url


# ---------------------------------------------------------------------------
# Diff-parsing helpers (moved from parse_changed_files.py)
# ---------------------------------------------------------------------------

def extract_function_name(context_line: str) -> str | None:
    """Extract a function/class name from a Git hunk header context line."""
    patterns = [
        # Python: def func_name(...) / async def ... / class ClassName
        r"(?:async\s+)?def\s+(\w+)",
        r"class\s+(\w+)",
        # JavaScript / TypeScript
        r"function\s+(\w+)",
        r"(?:const|let|var)\s+(\w+)\s*=\s*(?:async\s+)?(?:function|\()",
        # Go
        r"func\s+(?:\(\w+\s+\*?\w+\)\s+)?(\w+)",
        # Rust
        r"(?:pub\s+)?fn\s+(\w+)",
    ]
    for pattern in patterns:
        match = re.search(pattern, context_line)
        if match:
            return match.group(1)
    return None


def extract_definition_name(line: str) -> str | None:
    """Extract a function/class name from a definition line."""
    patterns = [
        r"^\s*(?:async\s+)?def\s+(\w+)",
        r"^\s*class\s+(\w+)",
        r"^\s*(?:export\s+)?(?:async\s+)?function\s+(\w+)",
        r"^\s*func\s+(?:\(\w+\s+\*?\w+\)\s+)?(\w+)",
        r"^\s*(?:pub\s+)?fn\s+(\w+)",
    ]
    for pattern in patterns:
        match = re.search(pattern, line)
        if match:
            return match.group(1)
    return None


# ---------------------------------------------------------------------------
# Internal: parse a raw diff into structured per-file data
# ---------------------------------------------------------------------------

def _parse_diff(diff_text: str) -> list[dict]:
    """Parse a unified diff into a list of per-file change dicts.

    Returns a list of dicts with keys: path, change_type, functions_touched,
    additions, deletions, hunk_ranges.

    If parsing fails or the diff is empty, returns an empty list.
    """
    if not diff_text or not diff_text.strip():
        return []

    try:
        patch = PatchSet(diff_text)
    except Exception:
        return []

    files: list[dict] = []

    for patched_file in patch:
        # Determine change type
        if patched_file.is_added_file:
            change_type = "added"
        elif patched_file.is_removed_file:
            change_type = "deleted"
        elif patched_file.is_rename:
            change_type = "renamed"
        else:
            change_type = "modified"

        # Use target path (b/...) for the file path
        path = patched_file.path
        if path.startswith("b/"):
            path = path[2:]
        elif path.startswith("a/"):
            path = path[2:]

        # Extract hunks + function names
        hunk_ranges: list[dict] = []
        functions_touched: set[str] = set()
        additions = 0
        deletions = 0

        for hunk in patched_file:
            hunk_ranges.append({
                "start": hunk.target_start,
                "end": hunk.target_start + hunk.target_length,
            })

            additions += hunk.added
            deletions += hunk.removed

            # Scan hunk header context lines for enclosing function names
            hunk_lines = str(hunk).splitlines()
            for line in hunk_lines:
                header_match = re.match(r"^@@\s+[^@]+@@\s*(.*)", line)
                if header_match:
                    context = header_match.group(1).strip()
                    fn = extract_function_name(context)
                    if fn:
                        functions_touched.add(fn)

            # Scan added/removed lines for definitions
            for line in hunk:
                line_str = str(line.value)
                if line.is_added or line.is_removed:
                    fn = extract_definition_name(line_str)
                    if fn:
                        functions_touched.add(fn)

        files.append({
            "path": path,
            "change_type": change_type,
            "functions_touched": sorted(functions_touched),
            "additions": additions,
            "deletions": deletions,
            "hunk_ranges": hunk_ranges,
        })

    return files


# ---------------------------------------------------------------------------
# Internal: resolve backend context from PR URL or environment
# ---------------------------------------------------------------------------

def _is_local_mode() -> bool:
    """Check whether we should use local mode."""
    mode = os.environ.get("SOURCE_MODE", "").lower()
    if mode == "local":
        return True
    if mode == "api":
        return False
    return os.environ.get("GITHUB_ACTIONS", "").lower() == "true"


# ---------------------------------------------------------------------------
# Tool function
# ---------------------------------------------------------------------------

def fetch_pr_diff(pr_url: str, tool_context: ToolContext) -> dict:
    """
    Fetches a GitHub PR's metadata and diff, returning a structured summary.

    The raw diff is parsed internally — only compact per-file summaries are
    returned.  This avoids sending large diff text through the LLM context.

    In GitHub Actions (local) mode, the pr_url parameter is still accepted
    but data is read from the local checkout and event payload instead of
    the GitHub API.

    Args:
        pr_url: The full GitHub PR URL
                (e.g., "https://github.com/owner/repo/pull/123").

    Returns:
        dict with keys:
            - status: "success" or "error"
            - pr_number: int
            - pr_title: str
            - pr_body: str (PR description)
            - files_changed: list of per-file dicts with path, change_type,
              functions_touched, additions, deletions, hunk_ranges
            - total_additions: int
            - total_deletions: int
    """
    # In API mode we need to parse the PR URL for owner/repo/number.
    # In local mode we still parse it (if valid) but fall back to env vars.
    owner, repo, pr_number = "", "", 0
    try:
        owner, repo, pr_number = parse_pr_url(pr_url)
    except ValueError as e:
        if not _is_local_mode():
            return {"status": "error", "error_message": str(e)}
        # In local mode, a missing/invalid URL is fine — we use env vars

    # Get the backend singleton and configure it with PR context
    backend = get_backend()
    if not _is_local_mode() and hasattr(backend, "configure"):
        backend.configure(owner=owner, repo=repo, pr_number=pr_number)

    # Store repo info in session state for downstream tools
    if _is_local_mode():
        repo_slug = os.environ.get("GITHUB_REPOSITORY", "")
        tool_context.state["repo"] = repo_slug or f"{owner}/{repo}"
    else:
        tool_context.state["repo"] = f"{owner}/{repo}"
    tool_context.state["pr_number"] = pr_number
    tool_context.state["pr_url"] = pr_url

    try:
        # Fetch PR metadata
        meta = backend.get_pr_metadata()
        if meta.get("number"):
            pr_number = meta["number"]
            tool_context.state["pr_number"] = pr_number
        if meta.get("html_url"):
            tool_context.state["pr_url"] = meta["html_url"]
        if meta.get("repo"):
            tool_context.state["repo"] = meta["repo"]

        # Fetch raw unified diff
        diff_text = backend.get_pr_diff()

        # Fetch file list (used as fallback if diff parsing fails)
        api_files = backend.get_pr_files()

        # Parse the diff into structured per-file data
        parsed_files = _parse_diff(diff_text)

        if parsed_files:
            # Use the rich parsed data
            files_changed = parsed_files
            total_additions = sum(f["additions"] for f in parsed_files)
            total_deletions = sum(f["deletions"] for f in parsed_files)
        else:
            # Fallback: use the file list data
            files_changed = [
                {
                    "path": f["filename"],
                    "change_type": f.get("status", "modified"),
                    "functions_touched": [],
                    "additions": f.get("additions", 0),
                    "deletions": f.get("deletions", 0),
                    "hunk_ranges": [],
                }
                for f in api_files
            ]
            total_additions = sum(f.get("additions", 0) for f in api_files)
            total_deletions = sum(f.get("deletions", 0) for f in api_files)

        # Write changed_files to session state for downstream agents
        tool_context.state["changed_files"] = [
            {
                "path": f["path"],
                "change_type": f["change_type"],
                "functions_touched": f["functions_touched"],
            }
            for f in files_changed
        ]

        return {
            "status": "success",
            "pr_number": pr_number,
            "pr_title": meta.get("title", ""),
            "pr_body": meta.get("body", "") or "",
            "files_changed": files_changed,
            "total_additions": total_additions,
            "total_deletions": total_deletions,
        }

    except Exception as e:
        return {"status": "error", "error_message": f"Failed to fetch PR data: {e}"}
