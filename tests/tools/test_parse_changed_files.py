"""Tests for diff-parsing helper functions.

The helpers extract_function_name and extract_definition_name were
originally in parse_changed_files.py and now live in fetch_pr_diff.py.
The tool-level parsing tests have moved to test_fetch_pr_diff.py
(TestParseDiff class).
"""

from pr_docs_reviewer.tools.fetch_pr_diff import (
    extract_function_name,
    extract_definition_name,
)


class TestExtractFunctionName:
    """Tests for the helper that extracts function names from context lines."""

    def test_python_def(self):
        assert extract_function_name("def process_data(x, y):") == "process_data"

    def test_python_async_def(self):
        assert extract_function_name("async def fetch(url):") == "fetch"

    def test_python_class(self):
        assert extract_function_name("class MyClient:") == "MyClient"

    def test_javascript_function(self):
        assert extract_function_name("function handleClick(event) {") == "handleClick"

    def test_go_func(self):
        assert extract_function_name("func (s *Server) Start() error {") == "Start"

    def test_rust_fn(self):
        assert extract_function_name("pub fn connect(addr: &str) -> Result<()>") == "connect"

    def test_no_match(self):
        assert extract_function_name("x = 42") is None

    def test_empty(self):
        assert extract_function_name("") is None


class TestExtractDefinitionName:
    """Tests for the helper that extracts names from definition lines."""

    def test_python_def(self):
        assert extract_definition_name("    def my_func(self):") == "my_func"

    def test_python_class(self):
        assert extract_definition_name("class Config:") == "Config"

    def test_not_a_call(self):
        """Should not match function calls, only definitions."""
        assert extract_definition_name("    result = my_func()") is None

    def test_comment_line(self):
        assert extract_definition_name("# def not_real():") is None
