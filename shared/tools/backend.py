"""Backend abstraction for repository data access.

Defines the RepoBackend protocol and a factory function that selects
the appropriate backend (GitHub API or local filesystem) based on
environment variables.

Mode selection logic:
    1. SOURCE_MODE=api  → GitHubAPIBackend (explicit override)
    2. SOURCE_MODE=local → LocalBackend    (explicit override)
    3. GITHUB_ACTIONS=true → LocalBackend  (auto-detect CI)
    4. Otherwise           → GitHubAPIBackend (default)
"""

from __future__ import annotations

import os
from typing import Protocol, runtime_checkable


@runtime_checkable
class RepoBackend(Protocol):
    """Protocol defining how the pipeline accesses repository data.

    Two implementations exist:
    - GitHubAPIBackend: fetches everything via GitHub REST API (needs GITHUB_TOKEN).
    - LocalBackend: reads from the local filesystem / git (needs a checkout).
    """

    # -- PR-level data -------------------------------------------------------

    def get_pr_metadata(self) -> dict:
        """Return PR metadata.

        Returns:
            dict with keys: title (str), body (str), number (int),
            html_url (str), repo (str "owner/repo").
        """
        ...

    def get_pr_diff(self) -> str:
        """Return the raw unified diff text for the PR."""
        ...

    def get_pr_files(self) -> list[dict]:
        """Return the list of changed files as fallback data.

        Each dict has keys: filename (str), status (str),
        additions (int), deletions (int).
        """
        ...

    # -- File-level data -----------------------------------------------------

    def read_file(self, path: str, ref: str = "HEAD") -> str:
        """Read the contents of a file at the given ref.

        Args:
            path: Repo-relative file path (e.g., "src/auth/login.py").
            ref: Git ref — branch, tag, or SHA.  Backends may ignore this
                 if the checkout already has the right state.

        Returns:
            File contents as a string.

        Raises:
            FileNotFoundError: If the file does not exist.
        """
        ...

    # -- File listing --------------------------------------------------------

    def list_files(
        self,
        path_prefix: str = "",
        extensions: list[str] | None = None,
    ) -> list[str]:
        """List files in the repository, optionally filtered by path and extension.

        Args:
            path_prefix: Only return files under this directory
                (e.g., "src/").  Empty string means the repo root.
            extensions: If given, only return files whose suffix matches
                one of these (e.g., [".py", ".ts"]).  The dot is required.

        Returns:
            Sorted list of repo-relative file paths.
        """
        ...

    # -- Search --------------------------------------------------------------

    def search_code(
        self,
        query: str,
        path_prefix: str = "",
        per_page: int = 20,
    ) -> list[dict]:
        """Search for *query* in files under *path_prefix*.

        Returns a list of dicts with keys:
            - path (str): repo-relative file path
            - text_matches (list[dict]): each with "fragment" (str)
        """
        ...

    # -- Write operations (for doc applier) ---------------------------------

    def get_pr_head_ref(self) -> tuple[str, str]:
        """Return (branch_name, head_sha) for the source PR.

        Example: ("feature/add-retry", "abc123def456...")
        """
        ...

    def create_branch(self, branch_name: str, sha: str) -> None:
        """Create a new branch pointing at the given SHA.

        Raises:
            RuntimeError: If the branch already exists (409) or API fails.
        """
        ...

    def write_file(
        self,
        path: str,
        content: str,
        message: str,
        branch: str,
    ) -> None:
        """Write (create or update) a file on the given branch.

        The implementation must handle fetching the current blob SHA
        for updates (GitHub API requires it) or directly writing (local git).

        Args:
            path: Repo-relative file path.
            content: New file content (plain text, not base64).
            message: Commit message.
            branch: Target branch name.
        """
        ...

    def create_pull_request(
        self,
        title: str,
        body: str,
        head: str,
        base: str,
    ) -> dict:
        """Open a new pull request.

        Returns:
            dict with keys: number (int), html_url (str).
        """
        ...


# ---------------------------------------------------------------------------
# Singleton + factory
# ---------------------------------------------------------------------------

_backend_instance: RepoBackend | None = None


def get_backend() -> RepoBackend:
    """Return the active RepoBackend, creating it on first call.

    The instance is cached as a module-level singleton so that all tools
    within a single pipeline run share the same backend.
    """
    global _backend_instance
    if _backend_instance is not None:
        return _backend_instance

    mode = os.environ.get("SOURCE_MODE", "").lower()

    if mode == "api":
        from .github_api_backend import GitHubAPIBackend
        _backend_instance = GitHubAPIBackend()
    elif mode == "local":
        from .local_backend import LocalBackend
        _backend_instance = LocalBackend()
    elif os.environ.get("GITHUB_ACTIONS", "").lower() == "true":
        from .local_backend import LocalBackend
        _backend_instance = LocalBackend()
    else:
        from .github_api_backend import GitHubAPIBackend
        _backend_instance = GitHubAPIBackend()

    return _backend_instance


def reset_backend() -> None:
    """Reset the cached backend (useful for testing)."""
    global _backend_instance
    _backend_instance = None


def set_backend(backend: RepoBackend) -> None:
    """Inject a specific backend instance (useful for testing)."""
    global _backend_instance
    _backend_instance = backend
