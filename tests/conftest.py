"""Shared test fixtures."""

from pathlib import Path
from unittest.mock import MagicMock

import pytest


FIXTURES_DIR = Path(__file__).parent / "fixtures"


@pytest.fixture
def sample_diff():
    """Load the single-file sample diff."""
    return (FIXTURES_DIR / "sample_diff.patch").read_text()


@pytest.fixture
def multi_file_diff():
    """Load the multi-file sample diff."""
    return (FIXTURES_DIR / "multi_file_diff.patch").read_text()


@pytest.fixture
def sample_python_source():
    """Load the sample Python source file."""
    return (FIXTURES_DIR / "sample_python_file.py").read_text()


@pytest.fixture
def sample_doc():
    """Load the sample documentation file."""
    return (FIXTURES_DIR / "sample_doc.md").read_text()


@pytest.fixture
def mock_tool_context():
    """Create a mock ToolContext with a state dict."""
    ctx = MagicMock()
    ctx.state = {}
    ctx.actions = MagicMock()
    ctx.actions.escalate = None
    return ctx
