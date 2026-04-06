"""PR Documentation Reviewer — Tools package.

Re-exports shared tools from shared.tools for backward compatibility,
plus PR-specific tools defined in this package.
"""

# Shared tools (re-exported for backward compatibility)
from shared.tools import (
    read_file_contents,
    get_function_signatures,
    search_docs_by_keyword,
    search_docs_by_file_reference,
    read_doc_file,
    apply_doc_updates,
    apply_suggestions,
    list_source_files,
)

# PR-specific tools
from .fetch_pr_diff import fetch_pr_diff

__all__ = [
    "fetch_pr_diff",
    "read_file_contents",
    "get_function_signatures",
    "search_docs_by_keyword",
    "search_docs_by_file_reference",
    "read_doc_file",
    "apply_doc_updates",
    "apply_suggestions",
    "list_source_files",
]
