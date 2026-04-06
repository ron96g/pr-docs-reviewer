"""Apply approved documentation suggestions by creating a branch, committing
changes, and opening a PR against the source PR's head branch.

Each suggestion now contains the complete updated file content produced by
the ``doc_writer`` agent, so the apply logic is a simple file-write — no
text-matching or replacement heuristics are needed.

This tool is called deterministically from ``run_pipeline.py`` and delegates
all Git/GitHub operations to the active :class:`RepoBackend`.
"""

from __future__ import annotations

import logging
import re
import time

from google.adk.tools import ToolContext

from .backend import get_backend

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _strip_markdown_fences(text: str) -> str:
    """Strip markdown code fences (```json ... ```) from LLM output."""
    stripped = re.sub(r"^```(?:json)?\s*\n?", "", text.strip())
    stripped = re.sub(r"\n?```\s*$", "", stripped)
    return stripped.strip()


# ---------------------------------------------------------------------------
# Core function
# ---------------------------------------------------------------------------

def apply_suggestions(
    suggestions: list[dict] | str | None,
    pr_number: int | str | None,
    repo: str = "",
) -> dict:
    """Create a branch, commit doc changes, and open a PR.

    This is the standalone entry point that does not require an ADK
    ``ToolContext``.  It can be called directly from ``run_pipeline.py``
    for deterministic auto-apply, or indirectly via the thin
    ``apply_doc_updates`` wrapper when invoked as an ADK tool.

    Each suggestion dict is expected to have the schema::

        {
            "doc_path": "<path>",
            "changes_summary": "<what changed and why>",
            "original_content": "<full original file>",
            "suggested_content": "<full updated file>"
        }

    The ``suggested_content`` is written directly — no text-matching needed.

    Args:
        suggestions: List of suggestion dicts, a JSON string, or None.
        pr_number: The source PR number.
        repo: "owner/repo" string (informational only).

    Returns:
        A result dict with status, PR URL, and details.
    """
    import json as _json

    # ------------------------------------------------------------------
    # Guard: nothing to apply
    # ------------------------------------------------------------------
    if not suggestions:
        return {
            "status": "success",
            "message": "No suggestions to apply",
            "files_updated": [],
            "commit_count": 0,
        }

    # If suggestions is a string (LLM JSON output), try to parse
    if isinstance(suggestions, str):
        try:
            suggestions = _json.loads(_strip_markdown_fences(suggestions))
        except (_json.JSONDecodeError, TypeError):
            return {
                "status": "error",
                "error_message": "doc_suggestions could not be parsed as JSON",
            }

    backend = get_backend()

    # ------------------------------------------------------------------
    # 1. Get the source PR's head branch + SHA
    # ------------------------------------------------------------------
    try:
        head_ref, head_sha = backend.get_pr_head_ref()
    except Exception as exc:
        return {
            "status": "error",
            "error_message": f"Failed to get PR head ref: {exc}",
        }

    # ------------------------------------------------------------------
    # 2. Create a new branch from the PR's head SHA
    # ------------------------------------------------------------------
    branch_name = f"docs/update-for-pr-{pr_number}"
    try:
        backend.create_branch(branch_name, head_sha)
    except RuntimeError:
        # Branch already exists — append timestamp and retry
        branch_name = f"docs/update-for-pr-{pr_number}-{int(time.time())}"
        try:
            backend.create_branch(branch_name, head_sha)
        except Exception as exc:
            return {
                "status": "error",
                "error_message": f"Failed to create branch: {exc}",
            }
    except Exception as exc:
        return {
            "status": "error",
            "error_message": f"Failed to create branch: {exc}",
        }

    # ------------------------------------------------------------------
    # 3. Apply each suggestion (one per doc file)
    # ------------------------------------------------------------------
    files_updated: list[str] = []
    skipped_suggestions: list[dict] = []
    commit_count = 0

    for idx, suggestion in enumerate(suggestions):
        doc_path = suggestion.get("doc_path", "")
        suggested_content = suggestion.get("suggested_content", "")

        if not doc_path:
            skipped_suggestions.append({
                "index": idx,
                "reason": "missing doc_path",
            })
            continue

        if not suggested_content:
            skipped_suggestions.append({
                "index": idx,
                "doc_path": doc_path,
                "reason": "missing suggested_content",
            })
            continue

        # Ensure the file ends with exactly one trailing newline
        new_content = suggested_content.rstrip("\n") + "\n"

        # Commit the change
        try:
            backend.write_file(
                path=doc_path,
                content=new_content,
                message=f"docs: update {doc_path} for PR #{pr_number}",
                branch=branch_name,
            )
            commit_count += 1
            if doc_path not in files_updated:
                files_updated.append(doc_path)
        except Exception as exc:
            skipped_suggestions.append({
                "index": idx,
                "doc_path": doc_path,
                "reason": f"write error: {exc}",
            })
            continue

    # ------------------------------------------------------------------
    # 4. Open a PR (only if we committed at least one change)
    # ------------------------------------------------------------------
    if commit_count == 0:
        result_dict: dict = {
            "status": "success",
            "message": "All suggestions were skipped — no changes to commit",
            "files_updated": [],
            "commit_count": 0,
            "skipped_suggestions": skipped_suggestions,
        }
        return result_dict

    try:
        # Build PR body
        body_lines = [
            f"Automated documentation updates for #{pr_number}.\n",
            "## Changes\n",
        ]
        for s in suggestions:
            path = s.get("doc_path", "?")
            summary = s.get("changes_summary", "").strip()
            if summary:
                body_lines.append(f"- **{path}**: {summary}")
            else:
                body_lines.append(f"- **{path}**: updated")

        if skipped_suggestions:
            body_lines.append("\n## Skipped suggestions\n")
            for sk in skipped_suggestions:
                body_lines.append(
                    f"- index {sk.get('index')}: {sk.get('reason')}"
                )

        pr_body = "\n".join(body_lines)

        pr_result = backend.create_pull_request(
            title=f"docs: update documentation for PR #{pr_number}",
            body=pr_body,
            head=branch_name,
            base=head_ref,
        )
    except Exception as exc:
        return {
            "status": "partial",
            "error_message": f"Changes committed but PR creation failed: {exc}",
            "branch": branch_name,
            "files_updated": files_updated,
            "commit_count": commit_count,
            "skipped_suggestions": skipped_suggestions,
        }

    return {
        "status": "success",
        "doc_pr_url": pr_result["html_url"],
        "doc_pr_number": pr_result["number"],
        "branch": branch_name,
        "files_updated": files_updated,
        "commit_count": commit_count,
        "skipped_suggestions": skipped_suggestions,
    }


def apply_doc_updates(tool_context: ToolContext) -> dict:
    """ADK tool wrapper — reads from session state and delegates to
    :func:`apply_suggestions`.

    This thin wrapper exists so the function can still be registered as an
    ADK tool if needed in the future.
    """
    return apply_suggestions(
        suggestions=tool_context.state.get("doc_suggestions"),
        pr_number=tool_context.state.get("pr_number"),
        repo=tool_context.state.get("repo", ""),
    )
