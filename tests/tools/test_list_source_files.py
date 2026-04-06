"""Tests for the list_source_files shared tool."""

from unittest.mock import MagicMock, patch

import pytest

from shared.tools.list_source_files import list_source_files, _DEFAULT_SOURCE_EXTENSIONS


@pytest.fixture
def mock_tool_context():
    ctx = MagicMock()
    ctx.state = {}
    return ctx


class TestListSourceFiles:
    """Tests for the list_source_files ADK tool wrapper."""

    def test_returns_files_with_default_extensions(self, mock_tool_context):
        fake_files = ["src/main.py", "src/utils.py", "README.md"]
        mock_backend = MagicMock()
        mock_backend.list_files.return_value = fake_files

        with patch("shared.tools.list_source_files.get_backend", return_value=mock_backend):
            result = list_source_files(mock_tool_context)

        assert result["status"] == "success"
        assert result["files"] == fake_files
        assert result["count"] == 3
        mock_backend.list_files.assert_called_once_with(
            path_prefix="",
            extensions=_DEFAULT_SOURCE_EXTENSIONS,
        )

    def test_passes_path_prefix(self, mock_tool_context):
        mock_backend = MagicMock()
        mock_backend.list_files.return_value = ["src/foo.py"]

        with patch("shared.tools.list_source_files.get_backend", return_value=mock_backend):
            result = list_source_files(mock_tool_context, path_prefix="src/")

        assert result["status"] == "success"
        mock_backend.list_files.assert_called_once_with(
            path_prefix="src/",
            extensions=_DEFAULT_SOURCE_EXTENSIONS,
        )

    def test_parses_custom_extensions(self, mock_tool_context):
        mock_backend = MagicMock()
        mock_backend.list_files.return_value = []

        with patch("shared.tools.list_source_files.get_backend", return_value=mock_backend):
            result = list_source_files(mock_tool_context, extensions=".py,.ts,.go")

        assert result["status"] == "success"
        mock_backend.list_files.assert_called_once_with(
            path_prefix="",
            extensions=[".py", ".ts", ".go"],
        )

    def test_strips_whitespace_in_extensions(self, mock_tool_context):
        mock_backend = MagicMock()
        mock_backend.list_files.return_value = []

        with patch("shared.tools.list_source_files.get_backend", return_value=mock_backend):
            list_source_files(mock_tool_context, extensions=" .py , .ts , ")

        mock_backend.list_files.assert_called_once_with(
            path_prefix="",
            extensions=[".py", ".ts"],
        )

    def test_returns_error_on_exception(self, mock_tool_context):
        mock_backend = MagicMock()
        mock_backend.list_files.side_effect = RuntimeError("network failure")

        with patch("shared.tools.list_source_files.get_backend", return_value=mock_backend):
            result = list_source_files(mock_tool_context)

        assert result["status"] == "error"
        assert "network failure" in result["error_message"]

    def test_empty_repo_returns_zero_count(self, mock_tool_context):
        mock_backend = MagicMock()
        mock_backend.list_files.return_value = []

        with patch("shared.tools.list_source_files.get_backend", return_value=mock_backend):
            result = list_source_files(mock_tool_context)

        assert result["status"] == "success"
        assert result["files"] == []
        assert result["count"] == 0


class TestDefaultExtensions:
    """Verify the default extensions list is sensible."""

    def test_includes_python(self):
        assert ".py" in _DEFAULT_SOURCE_EXTENSIONS

    def test_includes_common_web(self):
        assert ".js" in _DEFAULT_SOURCE_EXTENSIONS
        assert ".ts" in _DEFAULT_SOURCE_EXTENSIONS
        assert ".tsx" in _DEFAULT_SOURCE_EXTENSIONS

    def test_includes_docs(self):
        assert ".md" in _DEFAULT_SOURCE_EXTENSIONS
        assert ".rst" in _DEFAULT_SOURCE_EXTENSIONS

    def test_includes_config(self):
        assert ".yaml" in _DEFAULT_SOURCE_EXTENSIONS
        assert ".toml" in _DEFAULT_SOURCE_EXTENSIONS
        assert ".json" in _DEFAULT_SOURCE_EXTENSIONS
