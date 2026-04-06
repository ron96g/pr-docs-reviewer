"""Re-export from shared.tools.apply_doc_updates for backward compatibility."""
from shared.tools.apply_doc_updates import *  # noqa: F401,F403

# Wildcard import skips _-prefixed names; import explicitly.
from shared.tools.apply_doc_updates import _strip_markdown_fences  # noqa: F401
