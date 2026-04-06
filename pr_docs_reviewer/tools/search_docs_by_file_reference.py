"""Tool: search_docs_by_file_reference — finds docs that reference source files."""

import re

from google.adk.tools import ToolContext

from .backend import get_backend


def search_docs_by_file_reference(
    source_file_paths: list[str],
    tool_context: ToolContext,
    docs_path: str = "docs/",
) -> dict:
    """
    Searches docs for references to specific source file paths or their
    derived identifiers (module names, class names).

    The actual search mechanism (GitHub Code Search API or local filesystem
    walk) is selected by the active backend.

    Args:
        source_file_paths: Source code file paths that changed in the PR.
        docs_path: Subdirectory to search within.

    Returns:
        dict with key "results" containing a list of:
            - doc_file_path: str
            - referenced_source: str (which source path it references)
            - reference_context: str (the line containing the reference)
            - line_number: int or null
    """
    repo = tool_context.state.get("repo")
    if not repo:
        return {
            "status": "error",
            "error_message": "No repository context. Run fetch_pr_diff first.",
        }

    if not source_file_paths:
        return {"results": []}

    backend = get_backend()
    results = []
    seen = set()  # (doc_path, source_path) dedup

    for source_path in source_file_paths:
        # Derive search terms from the file path
        search_terms = _derive_search_terms(source_path)

        for term in search_terms:
            if not term or len(term) < 3:
                continue

            try:
                items = backend.search_code(
                    query=term,
                    path_prefix=docs_path,
                    per_page=10,
                )

                for item in items:
                    doc_path = item["path"]

                    # Filter to doc files
                    if not _is_doc_file(doc_path):
                        continue

                    dedup_key = (doc_path, source_path)
                    if dedup_key in seen:
                        continue
                    seen.add(dedup_key)

                    context = ""
                    text_matches = item.get("text_matches", [])
                    if text_matches:
                        context = text_matches[0].get("fragment", "")[:200]

                    results.append({
                        "doc_file_path": doc_path,
                        "referenced_source": source_path,
                        "reference_context": context,
                        "line_number": None,
                    })

            except Exception:
                continue

    return {"results": results}


def _derive_search_terms(file_path: str) -> list[str]:
    """
    Derive multiple search terms from a source file path.

    For "src/http/client_pool.py", produces:
        - "src/http/client_pool.py"   (full path)
        - "client_pool"                (module name / stem)
        - "ClientPool"                 (PascalCase)
        - "client-pool"                (kebab-case, common in doc URLs)
    """
    terms = [file_path]

    # Extract stem (filename without extension)
    parts = file_path.rsplit("/", 1)
    filename = parts[-1] if len(parts) > 1 else file_path
    stem = filename.rsplit(".", 1)[0] if "." in filename else filename

    if stem and stem != file_path:
        terms.append(stem)

    # snake_case -> PascalCase
    pascal = _snake_to_pascal(stem)
    if pascal and pascal != stem:
        terms.append(pascal)

    # snake_case -> kebab-case
    kebab = stem.replace("_", "-")
    if kebab and kebab != stem:
        terms.append(kebab)

    return terms


def _snake_to_pascal(name: str) -> str:
    """Convert snake_case to PascalCase."""
    return "".join(word.capitalize() for word in name.split("_") if word)


def _is_doc_file(path: str) -> bool:
    """Check if a file path looks like a documentation file."""
    doc_extensions = {".md", ".mdx", ".rst", ".txt", ".adoc", ".asciidoc"}
    lower = path.lower()
    return any(lower.endswith(ext) for ext in doc_extensions)
