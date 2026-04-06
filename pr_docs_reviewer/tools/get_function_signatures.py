"""Re-export from shared.tools.get_function_signatures for backward compatibility."""
from shared.tools.get_function_signatures import *  # noqa: F401,F403

# Wildcard import skips _-prefixed names; import them explicitly for
# test code that imports them via this module path.
from shared.tools.get_function_signatures import (  # noqa: F401
    _extract_python_signatures,
    _extract_regex_signatures,
    _format_params,
    _format_function_sig,
    _format_bases,
    _first_line,
)
