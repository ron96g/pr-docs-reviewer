"""Tool: list_source_files — lists source files in the repository."""

from google.adk.tools import ToolContext

from .backend import get_backend


# Default extensions for common source code files.
_DEFAULT_SOURCE_EXTENSIONS: list[str] = [
    ".py", ".js", ".ts", ".tsx", ".jsx",
    ".go", ".rs", ".java", ".kt", ".scala",
    ".c", ".cpp", ".h", ".hpp",
    ".rb", ".php", ".swift", ".m",
    ".cs", ".fs",
    ".sh", ".bash",
    ".yaml", ".yml", ".toml", ".json",
    ".md", ".rst", ".txt",
]


def list_source_files(
    tool_context: ToolContext,
    path_prefix: str = "",
    extensions: str = "",
) -> dict:
    """
    Lists source files in the repository, optionally filtered by directory
    and file extensions.

    The repository is automatically determined from session state. The
    actual data source (GitHub API or local filesystem) is selected by
    the active backend.

    Args:
        path_prefix: Only return files under this directory
            (e.g., "src/").  Empty string means the repo root.
        extensions: Comma-separated list of file extensions to include
            (e.g., ".py,.ts,.go").  Each extension must start with a dot.
            If empty, a default set of common source file extensions is used.

    Returns:
        dict with keys:
            - status: "success" or "error"
            - files: list[str] (repo-relative file paths, sorted)
            - count: int (number of files returned)
            - error_message: str (only if status is "error")
    """
    try:
        backend = get_backend()

        ext_list: list[str] | None = None
        if extensions:
            ext_list = [e.strip() for e in extensions.split(",") if e.strip()]
        else:
            ext_list = _DEFAULT_SOURCE_EXTENSIONS

        files = backend.list_files(
            path_prefix=path_prefix,
            extensions=ext_list,
        )

        return {
            "status": "success",
            "files": files,
            "count": len(files),
        }

    except Exception as e:
        return {
            "status": "error",
            "error_message": f"Failed to list files: {e}",
        }
