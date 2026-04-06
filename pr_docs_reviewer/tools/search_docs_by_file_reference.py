"""Re-export from shared.tools.search_docs_by_file_reference for backward compatibility."""
from shared.tools.search_docs_by_file_reference import *  # noqa: F401,F403

# Wildcard import skips _-prefixed names; import explicitly.
from shared.tools.search_docs_by_file_reference import (  # noqa: F401
    _derive_search_terms,
    _is_doc_file,
    _snake_to_pascal,
)
