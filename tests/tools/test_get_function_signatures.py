"""Tests for get_function_signatures tool — Python AST extraction."""

import pytest
from unittest.mock import patch, MagicMock

from pr_docs_reviewer.tools.get_function_signatures import (
    get_function_signatures,
    _extract_python_signatures,
    _format_params,
)


class TestExtractPythonSignatures:
    """Tests for the Python AST-based signature extractor."""

    def test_simple_function(self):
        source = '''
def greet(name: str) -> str:
    """Say hello."""
    return f"Hello, {name}"
'''
        result = _extract_python_signatures(source)
        sigs = result["signatures"]
        assert len(sigs) == 1
        assert sigs[0]["name"] == "greet"
        assert sigs[0]["type"] == "function"
        assert "name: str" in sigs[0]["signature"]
        assert "-> str" in sigs[0]["signature"]
        assert sigs[0]["docstring_summary"] == "Say hello."

    def test_function_with_defaults(self):
        source = '''
def connect(host: str, port: int = 8080, *, timeout: float = 30.0) -> None:
    """Connect to a server."""
    pass
'''
        result = _extract_python_signatures(source)
        sig = result["signatures"][0]
        assert "port: int = 8080" in sig["signature"]
        assert "timeout: float = 30.0" in sig["signature"]

    def test_async_function(self):
        source = '''
async def fetch(url: str) -> bytes:
    """Fetch data from URL."""
    pass
'''
        result = _extract_python_signatures(source)
        sig = result["signatures"][0]
        assert sig["signature"].startswith("async def")
        assert sig["name"] == "fetch"

    def test_class_extraction(self):
        source = '''
class HttpClient:
    """An HTTP client for making requests."""
    pass
'''
        result = _extract_python_signatures(source)
        sigs = result["signatures"]
        assert len(sigs) == 1
        assert sigs[0]["name"] == "HttpClient"
        assert sigs[0]["type"] == "class"
        assert sigs[0]["docstring_summary"] == "An HTTP client for making requests."

    def test_class_with_bases(self):
        source = '''
class CustomError(ValueError):
    """A custom error type."""
    pass
'''
        result = _extract_python_signatures(source)
        sig = result["signatures"][0]
        assert "ValueError" in sig["signature"]

    def test_class_with_methods(self):
        source = '''
class Client:
    """HTTP client."""
    def __init__(self, url: str):
        self.url = url

    def get(self, path: str) -> dict:
        """Send GET request."""
        pass
'''
        result = _extract_python_signatures(source)
        names = [s["name"] for s in result["signatures"]]
        # Should have the class and its methods
        assert "Client" in names
        # Methods should be present (either as "get" or "Client.get")
        assert any("get" in n for n in names)
        assert any("__init__" in n for n in names)

    def test_no_docstring(self):
        source = '''
def bare_function(x):
    return x * 2
'''
        result = _extract_python_signatures(source)
        sig = result["signatures"][0]
        assert sig["docstring_summary"] is None

    def test_multiline_docstring_gets_first_line(self):
        source = '''
def documented(x: int) -> int:
    """Process the input value.

    This function takes an integer and processes it
    through a complex algorithm.

    Args:
        x: The input value.

    Returns:
        The processed value.
    """
    return x
'''
        result = _extract_python_signatures(source)
        sig = result["signatures"][0]
        assert sig["docstring_summary"] == "Process the input value."

    def test_kwargs(self):
        source = '''
def flexible(name: str, *args, **kwargs) -> None:
    pass
'''
        result = _extract_python_signatures(source)
        sig = result["signatures"][0]
        assert "*args" in sig["signature"]
        assert "**kwargs" in sig["signature"]

    def test_syntax_error(self):
        source = "def broken("
        result = _extract_python_signatures(source)
        assert result.get("status") == "error"
        assert "syntax" in result["error_message"].lower()

    def test_empty_source(self):
        result = _extract_python_signatures("")
        assert result["signatures"] == []

    def test_sorted_by_line_number(self):
        source = '''
def second():
    pass

class First:
    pass

def third():
    pass
'''
        result = _extract_python_signatures(source)
        line_numbers = [s["line_number"] for s in result["signatures"]]
        assert line_numbers == sorted(line_numbers)

    def test_full_fixture_file(self, sample_python_source):
        """Test against the full sample Python fixture file."""
        result = _extract_python_signatures(sample_python_source)
        sigs = result["signatures"]
        names = [s["name"] for s in sigs]

        # Should find the classes
        assert "ConnectionPool" in names
        assert "Connection" in names

        # Should find the factory function
        assert "create_pool" in names

        # Should find methods
        assert any("acquire" in n for n in names)
        assert any("send" in n for n in names)

        # create_pool should have its keyword-only params
        create_pool_sig = next(s for s in sigs if s["name"] == "create_pool")
        assert "ssl: bool = True" in create_pool_sig["signature"]
        assert "verify: bool = True" in create_pool_sig["signature"]


class TestGetFunctionSignaturesIntegration:
    """Integration tests that mock the file fetching."""

    def test_delegates_to_python_parser(self, sample_python_source, mock_tool_context):
        """get_function_signatures uses AST for .py files."""
        mock_tool_context.state["repo"] = "owner/repo"

        with patch(
            "shared.tools.get_function_signatures.read_file_contents"
        ) as mock_read:
            mock_read.return_value = {
                "status": "success",
                "content": sample_python_source,
                "size_bytes": len(sample_python_source),
            }

            result = get_function_signatures(
                "src/pool.py", mock_tool_context
            )

            assert "signatures" in result
            assert len(result["signatures"]) > 0
            mock_read.assert_called_once()

    def test_returns_error_on_file_not_found(self, mock_tool_context):
        """Propagates errors from read_file_contents."""
        mock_tool_context.state["repo"] = "owner/repo"

        with patch(
            "shared.tools.get_function_signatures.read_file_contents"
        ) as mock_read:
            mock_read.return_value = {
                "status": "error",
                "error_message": "File not found",
            }

            result = get_function_signatures(
                "nonexistent.py", mock_tool_context
            )

            assert result["status"] == "error"
