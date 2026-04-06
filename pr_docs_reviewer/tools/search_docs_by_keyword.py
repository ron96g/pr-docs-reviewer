"""Re-export from shared.tools.search_docs_by_keyword for backward compatibility."""
from shared.tools.search_docs_by_keyword import *  # noqa: F401,F403

# Wildcard import skips _-prefixed names; import explicitly.
from shared.tools.search_docs_by_keyword import _is_doc_file  # noqa: F401
