"""Tests for the backend abstraction layer — protocol, factory, auto-detection."""

import os
from unittest.mock import patch, MagicMock

import pytest

from pr_docs_reviewer.tools.backend import (
    RepoBackend,
    get_backend,
    reset_backend,
    set_backend,
)
from pr_docs_reviewer.tools.github_api_backend import GitHubAPIBackend
from pr_docs_reviewer.tools.local_backend import LocalBackend


@pytest.fixture(autouse=True)
def _clean_backend():
    """Reset backend singleton and SOURCE_MODE between every test."""
    reset_backend()
    yield
    reset_backend()


# ---------------------------------------------------------------------------
# Protocol compliance
# ---------------------------------------------------------------------------

class TestProtocolCompliance:
    """Both backends satisfy the RepoBackend protocol."""

    def test_github_api_backend_is_repo_backend(self):
        assert isinstance(GitHubAPIBackend(), RepoBackend)

    def test_local_backend_is_repo_backend(self, tmp_path):
        assert isinstance(LocalBackend(repo_root=tmp_path), RepoBackend)


# ---------------------------------------------------------------------------
# Factory / auto-detection
# ---------------------------------------------------------------------------

class TestGetBackend:
    """Tests for the get_backend() factory and mode selection."""

    def test_default_is_github_api(self):
        """No env vars → GitHubAPIBackend."""
        with patch.dict(os.environ, {}, clear=True):
            backend = get_backend()
        assert isinstance(backend, GitHubAPIBackend)

    def test_source_mode_api(self):
        with patch.dict(os.environ, {"SOURCE_MODE": "api"}, clear=True):
            backend = get_backend()
        assert isinstance(backend, GitHubAPIBackend)

    def test_source_mode_local(self):
        with patch.dict(os.environ, {"SOURCE_MODE": "local"}, clear=True):
            backend = get_backend()
        assert isinstance(backend, LocalBackend)

    def test_github_actions_auto_detect(self):
        with patch.dict(os.environ, {"GITHUB_ACTIONS": "true"}, clear=True):
            backend = get_backend()
        assert isinstance(backend, LocalBackend)

    def test_source_mode_overrides_github_actions(self):
        """SOURCE_MODE=api wins even when GITHUB_ACTIONS=true."""
        with patch.dict(
            os.environ,
            {"SOURCE_MODE": "api", "GITHUB_ACTIONS": "true"},
            clear=True,
        ):
            backend = get_backend()
        assert isinstance(backend, GitHubAPIBackend)

    def test_singleton_caching(self):
        """get_backend() returns the same instance on repeated calls."""
        with patch.dict(os.environ, {}, clear=True):
            b1 = get_backend()
            b2 = get_backend()
        assert b1 is b2

    def test_reset_clears_singleton(self):
        with patch.dict(os.environ, {}, clear=True):
            b1 = get_backend()
            reset_backend()
            b2 = get_backend()
        assert b1 is not b2

    def test_set_backend_injects_instance(self):
        mock = MagicMock()
        set_backend(mock)
        assert get_backend() is mock


# ---------------------------------------------------------------------------
# GitHubAPIBackend.configure
# ---------------------------------------------------------------------------

class TestGitHubAPIBackendConfigure:

    def test_configure_sets_fields(self):
        b = GitHubAPIBackend()
        b.configure(owner="acme", repo="widgets", pr_number=99)
        assert b.owner == "acme"
        assert b.repo == "widgets"
        assert b.pr_number == 99
