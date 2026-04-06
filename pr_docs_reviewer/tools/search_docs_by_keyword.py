"""Tool: search_docs_by_keyword — searches docs for keyword matches."""

from google.adk.tools import ToolContext

from .backend import get_backend


def search_docs_by_keyword(
    keywords: list[str],
    tool_context: ToolContext,
    docs_path: str = "docs/",
) -> dict:
    """
    Searches the repository's docs directory for files containing
    any of the given keywords.

    The actual search mechanism (GitHub Code Search API or local filesystem
    walk) is selected by the active backend.

    Args:
        keywords: Search terms — function names, class names, concepts.
        docs_path: Subdirectory to search within. Defaults to "docs/".

    Returns:
        dict with key "results" containing a list of:
            - file_path: str
            - matches: list of {keyword, line_number, context_line}
            - match_count: int
    """
    repo = tool_context.state.get("repo")
    if not repo:
        return {
            "status": "error",
            "error_message": "No repository context. Run fetch_pr_diff first.",
        }

    if not keywords:
        return {"results": []}

    backend = get_backend()
    all_results: dict[str, dict] = {}  # file_path -> {matches, ...}

    for keyword in keywords:
        if not keyword.strip():
            continue

        try:
            items = backend.search_code(
                query=keyword,
                path_prefix=docs_path,
                per_page=20,
            )

            for item in items:
                file_path = item["path"]

                # Filter to doc file extensions
                if not _is_doc_file(file_path):
                    continue

                if file_path not in all_results:
                    all_results[file_path] = {
                        "file_path": file_path,
                        "matches": [],
                        "match_count": 0,
                    }

                text_matches = item.get("text_matches", [])
                for tm in text_matches:
                    fragment = tm.get("fragment", "")
                    all_results[file_path]["matches"].append({
                        "keyword": keyword,
                        "line_number": None,
                        "context_line": fragment[:200],
                    })
                    all_results[file_path]["match_count"] += 1

                # If no text_matches, still record the hit
                if not text_matches:
                    all_results[file_path]["matches"].append({
                        "keyword": keyword,
                        "line_number": None,
                        "context_line": "",
                    })
                    all_results[file_path]["match_count"] += 1

        except Exception:
            # Don't fail the whole search because one keyword errored
            continue

    # Sort by match count (most relevant first)
    results = sorted(all_results.values(), key=lambda r: r["match_count"], reverse=True)

    return {"results": results}


def _is_doc_file(path: str) -> bool:
    """Check if a file path looks like a documentation file."""
    doc_extensions = {".md", ".mdx", ".rst", ".txt", ".adoc", ".asciidoc"}
    lower = path.lower()
    return any(lower.endswith(ext) for ext in doc_extensions)
