"""Tool: read_file_contents — reads a file from the repo at a specific ref."""

from google.adk.tools import ToolContext

from .backend import get_backend


def read_file_contents(
    file_path: str,
    tool_context: ToolContext,
    ref: str = "HEAD",
) -> dict:
    """
    Reads the contents of a file from the repository at a specific ref.

    The repository is automatically determined from the PR being analyzed
    (stored in session state).  The actual data source (GitHub API or local
    filesystem) is selected by the active backend.

    Args:
        file_path: Path to the file within the repo (e.g., "src/auth/login.py").
        ref: Git ref — branch name, tag, or commit SHA. Defaults to "HEAD".

    Returns:
        dict with keys:
            - status: "success" or "error"
            - content: str (decoded file contents)
            - size_bytes: int
            - error_message: str (only if status is "error")
    """
    repo = tool_context.state.get("repo")
    if not repo:
        return {
            "status": "error",
            "error_message": "No repository context. Run fetch_pr_diff first.",
        }

    try:
        backend = get_backend()
        content = backend.read_file(file_path, ref=ref)
        return {
            "status": "success",
            "content": content,
            "size_bytes": len(content.encode("utf-8")),
        }

    except FileNotFoundError:
        return {
            "status": "error",
            "error_message": f"File not found: {file_path} at ref {ref}",
        }

    except Exception as e:
        return {
            "status": "error",
            "error_message": f"Failed to read {file_path}: {e}",
        }
