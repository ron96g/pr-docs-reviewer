"""Re-export from shared.tools.read_doc_file for backward compatibility."""
from shared.tools.read_doc_file import *  # noqa: F401,F403

# Wildcard import skips _-prefixed names; import them explicitly for
# test code that imports them via this module path.
from shared.tools.read_doc_file import _parse_markdown_sections  # noqa: F401
