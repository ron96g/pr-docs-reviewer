# PR Documentation Reviewer — Testing Strategy

This document describes how to test the multi-agent PR documentation review system at every level, from individual tool functions to full pipeline quality evaluation.

---

## 1. Testing Layers

| Layer | What it tests | ADK features used | Speed |
|---|---|---|---|
| **L1: Tool unit tests** | Individual Python tool functions in isolation | Plain pytest, no ADK | Fast (ms) |
| **L2: Agent behavior tests** | Single agent calls the right tools and produces valid output | `InMemoryRunner`, `InMemorySessionService` | Medium (seconds, 1 LLM call) |
| **L3: Pipeline integration tests** | End-to-end pipeline with controlled inputs | `AgentEvaluator`, `.test.json` eval cases | Slow (10-30s, multiple LLM calls) |
| **L4: Quality evaluation** | Output quality judged by LLM-as-judge | `AgentEvaluator` with rubric-based metrics | Slow (adds judge LLM calls) |

Each layer catches different classes of bugs:

- **L1** catches regressions in parsing, API handling, and data transformation.
- **L2** catches broken agent instructions, missing tools, and incorrect state wiring.
- **L3** catches integration failures: state keys not flowing between agents, parallel race conditions, loop termination issues.
- **L4** catches quality problems: hallucinated parameter names, missed doc sections, tone mismatches — things that require judgment, not assertions.

---

## 2. Layer 1: Tool Unit Tests

Tools are plain Python functions. Test them with standard pytest, mocking external dependencies (GitHub API).

### Directory Structure

```
tests/
  tools/
    test_fetch_pr_diff.py
    test_parse_changed_files.py
    test_read_file_contents.py
    test_get_function_signatures.py
    test_search_docs_by_keyword.py
    test_search_docs_by_file_reference.py
    test_read_doc_file.py
  fixtures/
    sample_diff.patch
    sample_pr_metadata.json
    sample_python_file.py
    sample_doc.md
```

### What to test per tool

#### `fetch_pr_diff`

```python
import pytest
from unittest.mock import patch, MagicMock
from pr_docs_reviewer.tools import fetch_pr_diff

class TestFetchPrDiff:
    def test_valid_pr_url(self, mock_github_api):
        """Correctly parses owner/repo/number from URL and returns structured data."""
        result = fetch_pr_diff("https://github.com/owner/repo/pull/42")
        assert result["status"] == "success"
        assert result["pr_number"] == 42
        assert "diff" in result
        assert isinstance(result["files_changed"], list)

    def test_invalid_url_format(self):
        """Returns error dict for non-GitHub URLs."""
        result = fetch_pr_diff("https://gitlab.com/owner/repo/merge_requests/1")
        assert result["status"] == "error"
        assert "error_message" in result

    def test_pr_not_found(self, mock_github_api_404):
        """Returns error dict when GitHub returns 404."""
        result = fetch_pr_diff("https://github.com/owner/repo/pull/99999")
        assert result["status"] == "error"

    def test_rate_limited(self, mock_github_api_rate_limited):
        """Retries on 429 and eventually returns data or a clear error."""
        result = fetch_pr_diff("https://github.com/owner/repo/pull/42")
        # Should either succeed after retry or return a clear error
        assert result["status"] in ("success", "error")
```

#### `parse_changed_files`

This tool is pure logic (no API calls) — ideal for thorough unit testing.

```python
class TestParseChangedFiles:
    def test_simple_modification(self, sample_diff):
        """Parses a single modified file with one hunk."""
        result = parse_changed_files(sample_diff)
        assert len(result["files"]) == 1
        assert result["files"][0]["change_type"] == "modified"

    def test_extracts_function_names(self):
        """Extracts function names from @@ context lines and def statements."""
        diff = '''--- a/src/client.py
+++ b/src/client.py
@@ -10,6 +10,7 @@ class Client:
     def __init__(self, base_url, timeout=30):
+        self.max_retries = 3
         self.base_url = base_url
'''
        result = parse_changed_files(diff)
        assert "Client.__init__" in result["files"][0]["functions_touched"] or \
               "__init__" in result["files"][0]["functions_touched"]

    def test_added_file(self):
        """Detects new files (--- /dev/null)."""
        diff = '''--- /dev/null
+++ b/src/new_module.py
@@ -0,0 +1,5 @@
+def new_function():
+    pass
'''
        result = parse_changed_files(diff)
        assert result["files"][0]["change_type"] == "added"

    def test_deleted_file(self):
        """Detects deleted files (+++ /dev/null)."""
        diff = '''--- a/src/old_module.py
+++ /dev/null
@@ -1,5 +0,0 @@
-def old_function():
-    pass
'''
        result = parse_changed_files(diff)
        assert result["files"][0]["change_type"] == "deleted"

    def test_renamed_file(self):
        """Detects renamed files from the diff header."""
        diff = '''diff --git a/src/old_name.py b/src/new_name.py
similarity index 95%
rename from src/old_name.py
rename to src/new_name.py
--- a/src/old_name.py
+++ b/src/new_name.py
@@ -1,3 +1,3 @@
 def some_function():
-    return "old"
+    return "new"
'''
        result = parse_changed_files(diff)
        assert result["files"][0]["change_type"] == "renamed"

    def test_empty_diff(self):
        """Handles an empty diff string gracefully."""
        result = parse_changed_files("")
        assert result["files"] == []

    def test_multiple_files(self, sample_multi_file_diff):
        """Correctly separates multiple files from a single diff."""
        result = parse_changed_files(sample_multi_file_diff)
        assert len(result["files"]) > 1
        paths = [f["path"] for f in result["files"]]
        assert len(paths) == len(set(paths))  # no duplicates
```

#### `get_function_signatures`

```python
class TestGetFunctionSignatures:
    def test_python_function(self):
        """Extracts a plain function signature from Python source."""
        source = '''
def connect(host: str, port: int = 8080, *, timeout: float = 30.0) -> Connection:
    """Establishes a connection to the given host."""
    ...
'''
        # Mock read_file_contents to return this source
        result = get_function_signatures("src/net.py", "owner/repo")
        sigs = result["signatures"]
        assert len(sigs) == 1
        assert sigs[0]["name"] == "connect"
        assert "host: str" in sigs[0]["signature"]
        assert "timeout: float = 30.0" in sigs[0]["signature"]
        assert "Establishes a connection" in sigs[0]["docstring_summary"]

    def test_class_with_methods(self):
        """Extracts class and its methods as separate entries."""
        source = '''
class HttpClient:
    """An HTTP client."""
    def __init__(self, base_url: str):
        self.base_url = base_url

    def get(self, path: str) -> Response:
        """Send a GET request."""
        ...
'''
        result = get_function_signatures("src/http.py", "owner/repo")
        names = [s["name"] for s in result["signatures"]]
        assert "HttpClient" in names
        assert "__init__" in names or "HttpClient.__init__" in names
        assert "get" in names or "HttpClient.get" in names

    def test_non_python_file(self):
        """Falls back to regex extraction for non-Python files."""
        source = '''
function fetchData(url, options = {}) {
  // Fetches data from the API
  return fetch(url, options);
}
'''
        result = get_function_signatures("src/api.js", "owner/repo")
        assert len(result["signatures"]) >= 1
```

#### `read_doc_file`

```python
class TestReadDocFile:
    def test_parses_markdown_sections(self):
        """Correctly identifies heading hierarchy and line ranges."""
        content = """# API Reference

## Client

The main client class.

### Configuration

Set these options:

## Server

Server-side docs.
"""
        result = read_doc_file("docs/api.md")
        sections = result["sections"]
        headings = [s["heading"] for s in sections]
        assert "API Reference" in headings
        assert "Client" in headings
        assert "Configuration" in headings
        assert "Server" in headings

        # "Configuration" section ends before "Server"
        config = next(s for s in sections if s["heading"] == "Configuration")
        server = next(s for s in sections if s["heading"] == "Server")
        assert config["end_line"] < server["start_line"]

    def test_empty_doc(self):
        """Handles a doc file with no headings."""
        result = read_doc_file("docs/empty.md")
        assert result["sections"] == []
```

### Fixtures

Fixtures live in `tests/fixtures/` as real files. For example:

**`tests/fixtures/sample_diff.patch`** — a realistic unified diff from a real PR:

```diff
diff --git a/src/http/client.py b/src/http/client.py
index abc1234..def5678 100644
--- a/src/http/client.py
+++ b/src/http/client.py
@@ -15,7 +15,8 @@ class Client:
-    def __init__(self, base_url: str, timeout: int = 30):
+    def __init__(self, base_url: str, timeout: int = 30, max_retries: int = 3):
         self.base_url = base_url
         self.timeout = timeout
+        self.max_retries = max_retries
@@ -45,6 +46,15 @@ class Client:
     def request(self, method: str, path: str, **kwargs) -> Response:
-        return self._session.request(method, f"{self.base_url}{path}", **kwargs)
+        for attempt in range(self.max_retries):
+            try:
+                return self._session.request(method, f"{self.base_url}{path}", **kwargs)
+            except ConnectionError:
+                if attempt == self.max_retries - 1:
+                    raise
+                time.sleep(2 ** attempt)
```

**`tests/fixtures/sample_doc.md`** — a realistic doc file:

```markdown
# HTTP Client

## Installation

pip install our-http-client

## Configuration

The Client accepts `timeout` and `base_url` parameters.

- `base_url` (str): The base URL for all requests.
- `timeout` (int, default=30): Request timeout in seconds.

## Usage

from our_client import Client

client = Client("https://api.example.com")
response = client.request("GET", "/users")
```

### Mock Fixtures (conftest.py)

```python
# tests/conftest.py
import pytest
from unittest.mock import patch, MagicMock

@pytest.fixture
def mock_github_api():
    """Mocks httpx/requests calls to GitHub API with successful responses."""
    with patch("pr_docs_reviewer.tools.github_client.get") as mock_get:
        mock_get.return_value = MagicMock(
            status_code=200,
            json=lambda: {
                "number": 42,
                "title": "Add retry logic",
                "body": "Adds configurable retry with exponential backoff",
            },
        )
        yield mock_get

@pytest.fixture
def sample_diff():
    """Loads the sample diff fixture."""
    with open("tests/fixtures/sample_diff.patch") as f:
        return f.read()

@pytest.fixture
def sample_doc():
    """Loads the sample doc fixture."""
    with open("tests/fixtures/sample_doc.md") as f:
        return f.read()
```

---

## 3. Layer 2: Agent Behavior Tests

Test individual agents using ADK's `InMemoryRunner` to verify they call the correct tools and produce valid structured output.

### Setup

```python
from google.adk.runners import InMemoryRunner
from google.adk.agents import Agent

async def run_agent_with_state(agent, user_message, initial_state=None):
    """Helper: runs an agent with optional pre-seeded state."""
    runner = InMemoryRunner(agent=agent, app_name="test")

    # Create a session, optionally with initial state
    session = await runner.session_service.create_session(
        app_name="test",
        user_id="test_user",
        state=initial_state or {},
    )

    # Run the agent
    events = []
    async for event in runner.run_async(
        user_id="test_user",
        session_id=session.id,
        new_message=user_message,
    ):
        events.append(event)

    # Reload session to get updated state
    session = await runner.session_service.get_session(
        app_name="test",
        user_id="test_user",
        session_id=session.id,
    )

    return events, session.state
```

### What to test per agent

#### `pr_analyzer`

```python
import pytest
import json

@pytest.mark.asyncio
async def test_pr_analyzer_calls_both_tools(pr_analyzer_agent, mock_tools):
    """pr_analyzer should call fetch_pr_diff then parse_changed_files."""
    events, state = await run_agent_with_state(
        pr_analyzer_agent,
        "Review this PR: https://github.com/owner/repo/pull/42",
    )

    # Check tool calls
    tool_calls = [e for e in events if hasattr(e, "function_calls")]
    tool_names = []
    for e in events:
        if e.actions and e.actions.function_calls:
            tool_names.extend(fc.name for fc in e.actions.function_calls)

    assert "fetch_pr_diff" in tool_names
    assert "parse_changed_files" in tool_names

    # Check output was written to state
    assert "pr_summary" in state
    summary = json.loads(state["pr_summary"]) if isinstance(state["pr_summary"], str) else state["pr_summary"]
    assert "files" in summary or "pr_number" in summary

@pytest.mark.asyncio
async def test_pr_analyzer_output_structure(pr_analyzer_agent, mock_tools):
    """pr_analyzer output should match the expected schema."""
    events, state = await run_agent_with_state(
        pr_analyzer_agent,
        "Review this PR: https://github.com/owner/repo/pull/42",
    )

    summary = state["pr_summary"]
    # The output should mention files and changes — exact structure
    # depends on LLM output, but key fields should be present
    assert summary  # non-empty
```

#### `doc_finder`

```python
@pytest.mark.asyncio
async def test_doc_finder_uses_both_search_strategies(doc_finder_agent, mock_tools):
    """doc_finder should use keyword search AND file reference search."""
    initial_state = {
        "pr_summary": json.dumps({
            "files": [{"path": "src/http/client.py", "functions_touched": ["Client.__init__"]}],
            "overall_summary": "Added max_retries parameter",
        }),
        "changed_files": json.dumps([
            {"path": "src/http/client.py", "change_type": "modified"}
        ]),
    }

    events, state = await run_agent_with_state(
        doc_finder_agent,
        "Find relevant documentation",
        initial_state=initial_state,
    )

    tool_names = extract_tool_names(events)
    assert "search_docs_by_keyword" in tool_names
    assert "search_docs_by_file_reference" in tool_names
    assert "relevant_doc_sections" in state

@pytest.mark.asyncio
async def test_doc_finder_reads_matches(doc_finder_agent, mock_tools):
    """doc_finder should read doc files to verify matches are substantive."""
    # ... similar setup ...
    tool_names = extract_tool_names(events)
    assert "read_doc_file" in tool_names
```

#### `quality_reviewer` — testing escalation

```python
@pytest.mark.asyncio
async def test_reviewer_approves_good_suggestions(quality_reviewer_agent):
    """Reviewer should set escalate=True for high-quality suggestions."""
    initial_state = {
        "doc_suggestions": json.dumps([{
            "doc_path": "docs/http-client.md",
            "section": "Configuration",
            "change_type": "add_parameter_docs",
            "suggested_text": "- `max_retries` (int, default=3): ...",
            "rationale": "PR added max_retries to Client.__init__",
        }]),
        "code_analysis": json.dumps([{
            "function": "Client.__init__",
            "change_type": "new_parameter",
            "description": "Added max_retries (int, default=3)",
        }]),
        "relevant_doc_sections": json.dumps([{
            "doc_path": "docs/http-client.md",
            "section_heading": "Configuration",
            "reason_for_update": "Missing max_retries parameter",
        }]),
    }

    events, state = await run_agent_with_state(
        quality_reviewer_agent,
        "Review the documentation suggestions",
        initial_state=initial_state,
    )

    # Check for escalation (approval)
    escalation_events = [e for e in events if e.actions and e.actions.escalate]
    # The reviewer should approve accurate, complete suggestions
    assert "reviewer_feedback" in state
```

### Mocking Tools for Agent Tests

When testing agent *behavior* (does it call the right tools?), mock the tools to return canned responses. This avoids hitting GitHub and isolates the agent's decision-making.

```python
@pytest.fixture
def mock_fetch_pr_diff():
    """Returns a mock fetch_pr_diff that returns canned data."""
    def _mock(pr_url: str) -> dict:
        return {
            "status": "success",
            "pr_number": 42,
            "pr_title": "Add retry logic",
            "diff": SAMPLE_DIFF,
            "files_changed": ["src/http/client.py"],
            "additions": 15,
            "deletions": 3,
        }
    return _mock
```

Then build the agent with the mock tool substituted:

```python
@pytest.fixture
def pr_analyzer_agent(mock_fetch_pr_diff, mock_parse_changed_files):
    return Agent(
        name="pr_analyzer",
        model="gemini-2.0-flash",
        instruction="...",
        tools=[mock_fetch_pr_diff, mock_parse_changed_files],
        output_key="pr_summary",
    )
```

---

## 4. Layer 3: Pipeline Integration Tests

Test the full pipeline end-to-end using ADK's `AgentEvaluator` with `.test.json` eval case files.

### Eval Case Format

ADK eval cases are JSON files with this structure:

```json
// tests/eval/pr_docs_pipeline.test.json
[
  {
    "name": "new_parameter_with_docs",
    "description": "PR adds a new parameter to a function that is documented",
    "input": "Review this PR: https://github.com/owner/repo/pull/42",
    "initial_session_state": {},
    "expected_tool_use": [
      {"tool_name": "fetch_pr_diff", "match_mode": "IN_ORDER"},
      {"tool_name": "parse_changed_files", "match_mode": "IN_ORDER"},
      {"tool_name": "read_file_contents", "match_mode": "ANY_ORDER"},
      {"tool_name": "search_docs_by_keyword", "match_mode": "ANY_ORDER"},
      {"tool_name": "read_doc_file", "match_mode": "ANY_ORDER"}
    ],
    "reference_output": "The documentation for the HTTP Client Configuration section needs updating to include the new max_retries parameter (int, default=3)."
  },
  {
    "name": "internal_refactor_no_doc_impact",
    "description": "PR is an internal refactoring with no user-facing changes",
    "input": "Review this PR: https://github.com/owner/repo/pull/99",
    "initial_session_state": {},
    "reference_output": "No documentation updates needed. The changes are internal refactoring with no impact on the public API or documented behavior."
  },
  {
    "name": "deleted_function",
    "description": "PR removes a function that is referenced in the docs",
    "input": "Review this PR: https://github.com/owner/repo/pull/55",
    "initial_session_state": {},
    "reference_output": "The documentation references the removed function `deprecated_connect()` in docs/api-reference.md. The section should be updated to remove the reference and point users to the replacement function `connect_v2()`."
  }
]
```

### Running Pipeline Tests

```python
# tests/eval/test_pipeline.py
import pytest
from google.adk.evaluation import AgentEvaluator
from pr_docs_reviewer.agent import pipeline  # the SequentialAgent

@pytest.fixture
def test_config():
    return {
        "metrics": [
            "tool_trajectory_avg_score",
            "response_match_score",
        ],
        "thresholds": {
            "tool_trajectory_avg_score": 0.7,
            "response_match_score": 0.6,
        },
    }

def test_pipeline_eval(test_config):
    """Run the pipeline against all eval cases."""
    AgentEvaluator.evaluate(
        agent_module="pr_docs_reviewer.agent",
        agent_name="pr_docs_pipeline",
        eval_dataset_file_path_or_dir="tests/eval/pr_docs_pipeline.test.json",
        **test_config,
    )
```

### What Integration Tests Catch

- **State wiring bugs**: If `code_mapper` writes to `code_analysis` but `doc_writer` reads from `code_analyses` (typo), the writer will get no input and produce garbage. The eval case will fail because the output won't match the reference.
- **Parallel race conditions**: If both `code_mapper` and `doc_finder` write to the same state key, one will overwrite the other. The eval case will catch this because the output will be incomplete.
- **Loop termination**: If the reviewer never escalates (e.g., instructions are too strict), the loop will hit `max_iterations=3` and the output will be the last draft, which may not pass quality thresholds.
- **Tool ordering**: The `tool_trajectory_avg_score` metric with `IN_ORDER` matching verifies that `fetch_pr_diff` is called before `parse_changed_files` (since the second needs the first's output).

---

## 5. Layer 4: Quality Evaluation

Use ADK's LLM-as-judge metrics to evaluate the *quality* of the documentation suggestions, not just their structural correctness.

### Relevant Built-in Metrics

| Metric | What it evaluates | Why it matters here |
|---|---|---|
| `hallucinations_v1` | Whether the response contains claims not grounded in the input | Critical: doc suggestions must not hallucinate parameter names, defaults, or behavior |
| `rubric_based_final_response_quality_v1` | Overall quality scored against a rubric | General quality gate for the final suggestions |
| `rubric_based_tool_use_quality_v1` | Whether tools were used effectively | Catches agents that call tools unnecessarily or miss needed tool calls |
| `safety_v1` | Whether output contains harmful content | Basic safety guardrail |

### Hallucination Detection

Hallucination is the highest-risk failure mode for this system. A doc suggestion that invents a parameter name or states the wrong default value is worse than no suggestion at all.

```python
# tests/eval/test_quality.py
def test_no_hallucinations():
    """Verify doc suggestions don't hallucinate details not in the code changes."""
    AgentEvaluator.evaluate(
        agent_module="pr_docs_reviewer.agent",
        agent_name="pr_docs_pipeline",
        eval_dataset_file_path_or_dir="tests/eval/hallucination_cases.test.json",
        metrics=["hallucinations_v1"],
        thresholds={"hallucinations_v1": 0.9},  # high bar
    )
```

### Custom Rubric for Doc Quality

ADK's `rubric_based_final_response_quality_v1` accepts a custom rubric. Define one specific to documentation quality:

```json
{
  "name": "doc_suggestion_hallucination_trap",
  "description": "The PR changes default timeout from 30 to 60 seconds. A planted doc section mentions max_retries which is NOT in this PR.",
  "input": "Review this PR: https://github.com/owner/repo/pull/200",
  "reference_output": "Update the timeout default from 30 to 60 in docs/config.md. Do NOT mention max_retries as it was not changed in this PR.",
  "rubric": [
    "The suggestion correctly identifies the timeout default change from 30 to 60",
    "The suggestion does NOT mention max_retries or any other parameter not changed in this PR",
    "The suggestion references the specific doc file and section that needs updating",
    "The suggested text is concrete, not vague"
  ]
}
```

### Adversarial Test Cases

Design eval cases that specifically probe for common failure modes:

#### 1. Hallucination trap

```json
{
  "name": "hallucination_trap_unrelated_param",
  "description": "Only one parameter changed but the function has many. Agent must not suggest docs for unchanged parameters.",
  "input": "Review this PR that only changes the timeout default from 30 to 60",
  "reference_output": "Update timeout default documentation only. No other parameters were changed."
}
```

#### 2. No-op detection

```json
{
  "name": "noop_test_only_changes",
  "description": "PR only modifies test files — no doc updates needed",
  "input": "Review this PR: (test-only changes)",
  "reference_output": "No documentation updates needed."
}
```

#### 3. Deleted function still documented

```json
{
  "name": "deleted_function_in_docs",
  "description": "A function was deleted but docs still reference it",
  "input": "Review this PR that removes the deprecated_connect function",
  "reference_output": "docs/api.md references deprecated_connect which was removed. Suggest removing the section and adding a migration note."
}
```

#### 4. Renamed function

```json
{
  "name": "renamed_function",
  "description": "A function was renamed — docs should update all references",
  "input": "Review this PR that renames send_message to send_notification",
  "reference_output": "All references to send_message in docs should be updated to send_notification."
}
```

---

## 6. Testing the LoopAgent (Refinement Loop)

The refinement loop (`doc_writer` + `quality_reviewer`) needs specific tests to verify convergence behavior.

### Test: Loop exits on approval

```python
@pytest.mark.asyncio
async def test_refinement_loop_exits_on_approval():
    """The loop should exit early when the reviewer approves."""
    # Pre-seed state with perfect inputs so the reviewer approves on iteration 1
    initial_state = {
        "code_analysis": json.dumps([{
            "function": "Client.__init__",
            "change_type": "new_parameter",
            "description": "Added max_retries (int, default=3)",
            "user_facing_impact": "high",
        }]),
        "relevant_doc_sections": json.dumps([{
            "doc_path": "docs/http-client.md",
            "section_heading": "Configuration",
            "current_content_snippet": "The Client accepts timeout and base_url.",
            "reason_for_update": "Missing max_retries parameter",
        }]),
    }

    events, state = await run_agent_with_state(
        refinement_loop, "Generate documentation suggestions", initial_state
    )

    # Count how many times doc_writer was invoked
    writer_invocations = count_agent_invocations(events, "doc_writer")
    # With good inputs, should converge in 1-2 iterations, not 3
    assert writer_invocations <= 2
    assert "doc_suggestions" in state
```

### Test: Loop incorporates feedback

```python
@pytest.mark.asyncio
async def test_loop_incorporates_reviewer_feedback():
    """If the reviewer rejects, the next iteration's writer output should differ."""
    # Pre-seed with inputs that will cause a rejection on first pass
    # (e.g., doc section with specific formatting that the writer might miss)
    initial_state = { ... }

    events, state = await run_agent_with_state(
        refinement_loop, "Generate documentation suggestions", initial_state
    )

    # There should be reviewer_feedback in state
    assert "reviewer_feedback" in state

    # If the loop ran more than once, the second doc_suggestions should
    # be different from the first (indicating feedback was incorporated)
    suggestion_versions = extract_state_versions(events, "doc_suggestions")
    if len(suggestion_versions) > 1:
        assert suggestion_versions[0] != suggestion_versions[1]
```

### Test: Loop respects max_iterations

```python
@pytest.mark.asyncio
async def test_loop_terminates_at_max_iterations():
    """The loop must terminate after max_iterations even if never approved."""
    # Use a reviewer that always rejects (via strict mock instructions)
    strict_reviewer = Agent(
        name="quality_reviewer",
        model="gemini-2.0-flash",
        instruction="Always reject. Always find issues. Never approve.",
        output_key="reviewer_feedback",
    )

    test_loop = LoopAgent(
        name="refinement_loop",
        max_iterations=3,
        sub_agents=[doc_writer, strict_reviewer],
    )

    events, state = await run_agent_with_state(
        test_loop, "Generate docs", initial_state
    )

    writer_invocations = count_agent_invocations(events, "doc_writer")
    assert writer_invocations == 3  # ran exactly max_iterations times
    assert "doc_suggestions" in state  # still has output from last iteration
```

---

## 7. Test Configuration

### `tests/eval/test_config.json`

ADK's `AgentEvaluator` can read test configuration from a JSON file:

```json
{
  "agent_module": "pr_docs_reviewer.agent",
  "agent_name": "pr_docs_pipeline",
  "metrics": [
    "tool_trajectory_avg_score",
    "response_match_score",
    "hallucinations_v1",
    "rubric_based_final_response_quality_v1"
  ],
  "thresholds": {
    "tool_trajectory_avg_score": 0.7,
    "response_match_score": 0.5,
    "hallucinations_v1": 0.9,
    "rubric_based_final_response_quality_v1": 0.7
  }
}
```

### Running Tests

```bash
# Layer 1: Tool unit tests (fast, no LLM calls)
pytest tests/tools/ -v

# Layer 2: Agent behavior tests (needs LLM API key)
pytest tests/agents/ -v

# Layer 3+4: Pipeline integration + quality eval (slow, multiple LLM calls)
pytest tests/eval/ -v

# All layers
pytest tests/ -v

# Run with ADK's eval CLI (alternative to pytest)
adk eval pr_docs_reviewer --eval_set_file tests/eval/pr_docs_pipeline.test.json
```

---

## 8. Test Data Management

### Approach: Recorded Fixtures over Live API Calls

For repeatable, fast tests, **record** GitHub API responses once and replay them:

1. **Record**: Run tools against a real PR once, save the API responses as JSON fixtures.
2. **Replay**: In tests, mock the HTTP client to return the recorded responses.
3. **Refresh**: Periodically re-record to catch API changes.

This gives us:
- **Deterministic tests**: Same input always produces the same tool output.
- **No API dependency**: Tests run without `GITHUB_TOKEN`, without network access, and without rate limits.
- **Realistic data**: Fixtures are from real PRs, not hand-crafted approximations.

### Fixture Recording Script

```python
# scripts/record_fixtures.py
"""
Records GitHub API responses for a given PR into test fixtures.

Usage:
    python scripts/record_fixtures.py https://github.com/owner/repo/pull/42 tests/fixtures/pr_42/
"""
import sys
import json
import httpx
import os
from pathlib import Path

def record_pr_fixtures(pr_url: str, output_dir: str):
    owner, repo, pr_number = parse_pr_url(pr_url)
    token = os.environ["GITHUB_TOKEN"]
    headers = {"Authorization": f"Bearer {token}"}
    base = f"https://api.github.com/repos/{owner}/{repo}"
    output = Path(output_dir)
    output.mkdir(parents=True, exist_ok=True)

    # PR metadata
    r = httpx.get(f"{base}/pulls/{pr_number}", headers=headers)
    (output / "pr_metadata.json").write_text(json.dumps(r.json(), indent=2))

    # PR files
    r = httpx.get(f"{base}/pulls/{pr_number}/files", headers=headers)
    (output / "pr_files.json").write_text(json.dumps(r.json(), indent=2))

    # Raw diff
    r = httpx.get(
        f"{base}/pulls/{pr_number}",
        headers={**headers, "Accept": "application/vnd.github.v3.diff"},
    )
    (output / "pr_diff.patch").write_text(r.text)

    # File contents for each changed file
    files = json.loads((output / "pr_files.json").read_text())
    for f in files:
        r = httpx.get(f"{base}/contents/{f['filename']}?ref=HEAD", headers=headers)
        safe_name = f["filename"].replace("/", "__")
        (output / f"contents__{safe_name}.json").write_text(json.dumps(r.json(), indent=2))

    print(f"Recorded {len(files) + 3} fixtures to {output_dir}")
```

---

## 9. CI Integration

### GitHub Actions Workflow

```yaml
# .github/workflows/test.yml
name: Tests
on: [push, pull_request]

jobs:
  unit-tests:
    name: "L1: Tool Unit Tests"
    runs-on: ubuntu-latest
    steps:
      - uses: actions/checkout@v4
      - uses: actions/setup-python@v5
        with: { python-version: "3.13" }
      - run: pip install -e ".[test]"
      - run: pytest tests/tools/ -v

  agent-tests:
    name: "L2+L3: Agent & Pipeline Tests"
    runs-on: ubuntu-latest
    # Only run on PRs to main (costs money due to LLM calls)
    if: github.event_name == 'pull_request' && github.base_ref == 'main'
    env:
      GOOGLE_API_KEY: ${{ secrets.GOOGLE_API_KEY }}
    steps:
      - uses: actions/checkout@v4
      - uses: actions/setup-python@v5
        with: { python-version: "3.13" }
      - run: pip install -e ".[test]"
      - run: pytest tests/agents/ tests/eval/ -v --timeout=120

  quality-eval:
    name: "L4: Quality Evaluation"
    runs-on: ubuntu-latest
    # Only run on release branches (expensive)
    if: startsWith(github.ref, 'refs/heads/release/')
    env:
      GOOGLE_API_KEY: ${{ secrets.GOOGLE_API_KEY }}
    steps:
      - uses: actions/checkout@v4
      - uses: actions/setup-python@v5
        with: { python-version: "3.13" }
      - run: pip install -e ".[test]"
      - run: pytest tests/eval/test_quality.py -v --timeout=300
```

The key insight: **L1 tests run on every push** (free, fast), **L2/L3 tests run on PRs to main** (moderate cost), and **L4 quality evals run on release branches** (expensive but catches subtle quality regressions).

---

## 10. Summary

| Layer | Tests | Catches | Cost | Frequency |
|---|---|---|---|---|
| L1: Tool unit tests | ~30-50 tests | Parsing bugs, API handling, data transformation | Free | Every push |
| L2: Agent behavior | ~10-15 tests | Wrong tool calls, broken instructions, missing state | Low (1 LLM call each) | PRs to main |
| L3: Pipeline integration | ~5-10 eval cases | State wiring, parallel races, loop termination | Medium (full pipeline) | PRs to main |
| L4: Quality evaluation | ~5-10 eval cases | Hallucinations, tone mismatch, incomplete coverage | High (pipeline + judge) | Releases |

The testing pyramid is deliberate: many cheap, fast unit tests at the base; fewer expensive end-to-end tests at the top. The hallucination detection tests (L4) are the most important quality gate — a doc suggestion with invented details is actively harmful.
