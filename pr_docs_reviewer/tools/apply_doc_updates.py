"""Apply approved documentation suggestions by creating a branch, committing
changes, and opening a PR against the source PR's head branch.

This tool is called by the ``doc_applier`` LlmAgent and delegates all
Git/GitHub operations to the active :class:`RepoBackend`.
"""

from __future__ import annotations

import logging
import re
import time

from google.adk.tools import ToolContext

from .backend import get_backend

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Text replacement helpers
# ---------------------------------------------------------------------------

def _normalize_whitespace(text: str) -> str:
    """Collapse all runs of whitespace into single spaces and strip."""
    return re.sub(r"\s+", " ", text).strip()


def _apply_replacement(
    content: str,
    current_text: str,
    suggested_text: str,
) -> str | None:
    """Try to replace *current_text* with *suggested_text* in *content*.

    Strategy:
        1. Exact substring match.
        2. Normalised whitespace match — both sides are collapsed before
           comparison, but the replacement is inserted in place of the
           original span so surrounding formatting is preserved.

    Returns the updated content string, or ``None`` if neither strategy
    could locate *current_text*.
    """
    # 1. Exact match
    if current_text in content:
        return content.replace(current_text, suggested_text, 1)

    # 2. Normalised match — build a regex that treats any whitespace run
    #    in current_text as ``\s+`` so we can locate it in the raw content.
    norm_current = _normalize_whitespace(current_text)
    if not norm_current:
        return None

    # Escape each non-whitespace token and join with \s+
    tokens = norm_current.split()
    pattern = r"\s+".join(re.escape(t) for t in tokens)
    match = re.search(pattern, content)
    if match:
        return content[: match.start()] + suggested_text + content[match.end():]

    return None


# ---------------------------------------------------------------------------
# Tool function
# ---------------------------------------------------------------------------

def apply_doc_updates(tool_context: ToolContext) -> dict:
    """Create a branch, commit doc changes, and open a PR.

    Reads from session state:
        doc_suggestions — list of approved suggestion dicts.
        repo            — "owner/repo" string.
        pr_number       — int.
        pr_url          — str.

    Returns a result dict with status, PR URL, and details.
    """
    suggestions = tool_context.state.get("doc_suggestions")
    pr_number = tool_context.state.get("pr_number")
    repo = tool_context.state.get("repo", "")

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
        import json
        try:
            suggestions = json.loads(suggestions)
        except (json.JSONDecodeError, TypeError):
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
    # 3. Apply each suggestion
    # ------------------------------------------------------------------
    files_updated: list[str] = []
    skipped_suggestions: list[dict] = []
    commit_count = 0

    for idx, suggestion in enumerate(suggestions):
        doc_path = suggestion.get("doc_path", "")
        current_text = suggestion.get("current_text")
        suggested_text = suggestion.get("suggested_text", "")
        change_type = suggestion.get("change_type", "update_text")

        if not doc_path:
            skipped_suggestions.append({
                "index": idx,
                "reason": "missing doc_path",
            })
            continue

        # Read current file content
        try:
            file_content = backend.read_file(doc_path, ref=branch_name)
        except FileNotFoundError:
            skipped_suggestions.append({
                "index": idx,
                "doc_path": doc_path,
                "reason": "file not found",
            })
            continue
        except Exception as exc:
            skipped_suggestions.append({
                "index": idx,
                "doc_path": doc_path,
                "reason": f"read error: {exc}",
            })
            continue

        # Apply the text change
        if change_type in ("add_section", "add_note", "add_warning") or current_text is None:
            # For additions, append at the end of the file
            new_content = file_content.rstrip("\n") + "\n\n" + suggested_text + "\n"
        else:
            result = _apply_replacement(file_content, current_text, suggested_text)
            if result is None:
                skipped_suggestions.append({
                    "index": idx,
                    "doc_path": doc_path,
                    "reason": "could not locate target text in file",
                })
                continue
            new_content = result

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
        result: dict = {
            "status": "success",
            "message": "All suggestions were skipped — no changes to commit",
            "files_updated": [],
            "commit_count": 0,
            "skipped_suggestions": skipped_suggestions,
        }
        return result

    try:
        # Build PR body
        body_lines = [
            f"Automated documentation updates for #{pr_number}.\n",
            "## Changes\n",
        ]
        for s in suggestions:
            path = s.get("doc_path", "?")
            rationale = s.get("rationale", "")
            body_lines.append(f"- **{path}**: {rationale}")

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
