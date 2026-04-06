"""Re-export from shared.tools.local_backend for backward compatibility."""
from shared.tools.local_backend import *  # noqa: F401,F403

# Wildcard import doesn't re-export stdlib modules that tests patch via
# "pr_docs_reviewer.tools.local_backend.subprocess".  Import explicitly.
import subprocess  # noqa: F401
