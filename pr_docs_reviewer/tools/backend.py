"""Re-export from shared.tools.backend for backward compatibility."""
from shared.tools.backend import *  # noqa: F401,F403

# Wildcard import skips _-prefixed names; import them explicitly for
# test code that may reference them.
from shared.tools.backend import _backend_instance  # noqa: F401
