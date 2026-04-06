"""Integration test: verify state propagation across the sequential pipeline.

Uses before_model_callback to mock LLM responses, so no real API calls are
made.  The key thing we're testing is that tool-level state writes
(tool_context.state["changed_files"]) made during pr_analyzer's execution
are visible to downstream agents (code_mapper, doc_finder) when their
instructions are resolved via {changed_files} template injection.
"""

import json
from unittest.mock import patch, MagicMock

import pytest

from google.adk.agents import Agent, SequentialAgent, ParallelAgent
from google.adk.agents.callback_context import CallbackContext
from google.adk.models.llm_request import LlmRequest
from google.adk.models.llm_response import LlmResponse
from google.adk.runners import InMemoryRunner
from google.genai import types

from pr_docs_reviewer.tools.backend import reset_backend, set_backend


# ---------------------------------------------------------------------------
# Fake GitHub API responses
# ---------------------------------------------------------------------------

_FAKE_PR_META = {"number": 1, "title": "Add feature", "body": "test PR"}
_FAKE_PR_FILES = [
    {"filename": "src/client.py", "additions": 5, "deletions": 1, "status": "modified"},
    {"filename": "src/utils.py", "additions": 10, "deletions": 0, "status": "added"},
]
_FAKE_DIFF_TEXT = """\
diff --git a/src/client.py b/src/client.py
index abc1234..def5678 100644
--- a/src/client.py
+++ b/src/client.py
@@ -10,2 +10,5 @@ class Client:
     def connect(self):
-        pass
+        self._retry()
+
+    def _retry(self):
+        pass
"""


def _make_mock_backend():
    """Create a mock backend with standard fake responses."""
    backend = MagicMock()
    backend.get_pr_metadata.return_value = {
        "number": 1,
        "title": "Add feature",
        "body": "test PR",
        "html_url": "https://github.com/owner/repo/pull/1",
        "repo": "owner/repo",
    }
    backend.get_pr_diff.return_value = _FAKE_DIFF_TEXT
    backend.get_pr_files.return_value = _FAKE_PR_FILES
    return backend


@pytest.fixture(autouse=True)
def _reset_backend():
    """Ensure backend singleton is reset between tests."""
    reset_backend()
    yield
    reset_backend()


# ---------------------------------------------------------------------------
# Mock LLM callback — returns canned responses + tool calls
# ---------------------------------------------------------------------------

def _make_mock_callback():
    """Factory for a mock LLM callback that tracks call count.

    After the bottleneck fix, pr_analyzer only needs 2 LLM calls:
      call 0: function_call to fetch_pr_diff
      call 1: final text summary (no more parse_changed_files call)
    """
    call_count = {"n": 0}

    def mock_llm(*, callback_context: CallbackContext, llm_request: LlmRequest) -> LlmResponse:
        """Simulate LLM behavior for each agent step."""
        agent_name = callback_context.agent_name

        if agent_name == "pr_analyzer":
            if call_count["n"] == 0:
                # First call: tell the LLM to call fetch_pr_diff
                call_count["n"] += 1
                return LlmResponse(
                    content=types.Content(
                        role="model",
                        parts=[types.Part(
                            function_call=types.FunctionCall(
                                name="fetch_pr_diff",
                                args={"pr_url": "https://github.com/owner/repo/pull/1"},
                            )
                        )],
                    ),
                )
            else:
                # Second call: return the final summary text
                call_count["n"] += 1
                return LlmResponse(
                    content=types.Content(
                        role="model",
                        parts=[types.Part(text=json.dumps({
                            "pr_number": 1,
                            "pr_title": "Add feature",
                            "overall_summary": "Adds retry logic to Client",
                            "files": [
                                {"path": "src/client.py", "change_type": "modified",
                                 "functions_touched": ["connect", "_retry"],
                                 "summary": "Added retry method"},
                            ],
                        }))],
                    ),
                )

        # For parallel agents (code_mapper, doc_finder), just return text
        # The key test here is that their instructions resolve without error
        return LlmResponse(
            content=types.Content(
                role="model",
                parts=[types.Part(text=f"[{agent_name}] analysis complete")],
            ),
        )

    return mock_llm


# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_state_propagation_pr_analyzer_to_research_phase():
    """changed_files written by fetch_pr_diff is visible to code_mapper/doc_finder."""
    from pr_docs_reviewer.tools import (
        fetch_pr_diff,
        read_file_contents,
        get_function_signatures,
        search_docs_by_keyword,
        search_docs_by_file_reference,
        read_doc_file,
    )

    mock_callback = _make_mock_callback()

    pr_analyzer = Agent(
        name="pr_analyzer",
        model="gemini-2.0-flash",
        instruction="Analyze the PR. Call fetch_pr_diff.",
        tools=[fetch_pr_diff],
        output_key="pr_summary",
        before_model_callback=mock_callback,
    )

    # Use simpler instructions that reference the same state keys
    code_mapper = Agent(
        name="code_mapper",
        model="gemini-2.0-flash",
        instruction="Analyze code changes. Changed files: {changed_files}",
        tools=[],
        output_key="code_analysis",
        before_model_callback=mock_callback,
    )

    doc_finder = Agent(
        name="doc_finder",
        model="gemini-2.0-flash",
        instruction="Find docs to update. PR summary: {pr_summary}. Changed files: {changed_files}",
        tools=[],
        output_key="relevant_doc_sections",
        before_model_callback=mock_callback,
    )

    research_phase = ParallelAgent(
        name="research_phase",
        sub_agents=[code_mapper, doc_finder],
    )

    pipeline = SequentialAgent(
        name="test_pipeline",
        sub_agents=[pr_analyzer, research_phase],
    )

    async with InMemoryRunner(agent=pipeline, app_name="test") as runner:
        session = await runner.session_service.create_session(
            app_name="test",
            user_id="u1",
            session_id="s1",
        )

        events = []
        set_backend(_make_mock_backend())
        async for event in runner.run_async(
            user_id="u1",
            session_id="s1",
            new_message=types.Content(
                role="user",
                parts=[types.Part(text="Review https://github.com/owner/repo/pull/1")],
            ),
        ):
            events.append(event)

        # Fetch session to inspect state
        session = await runner.session_service.get_session(
            app_name="test", user_id="u1", session_id="s1",
        )

        # Verify state was written
        assert "changed_files" in session.state, (
            f"changed_files not in state. Keys: {list(session.state.keys())}"
        )
        assert "pr_summary" in session.state, (
            f"pr_summary not in state. Keys: {list(session.state.keys())}"
        )

        # Verify changed_files has expected content (now rich, with functions_touched)
        changed = session.state["changed_files"]
        assert isinstance(changed, list)
        assert len(changed) >= 1
        paths = [f["path"] for f in changed]
        assert "src/client.py" in paths
        # functions_touched should be populated by the inline parser
        client_entry = next(f for f in changed if f["path"] == "src/client.py")
        assert len(client_entry["functions_touched"]) >= 1

        # Verify code_mapper and doc_finder ran (they produced output_key)
        assert "code_analysis" in session.state, (
            f"code_analysis not in state — code_mapper may not have run. Keys: {list(session.state.keys())}"
        )
        assert "relevant_doc_sections" in session.state, (
            f"relevant_doc_sections not in state — doc_finder may not have run. Keys: {list(session.state.keys())}"
        )

        print(f"\nState keys after pipeline: {list(session.state.keys())}")
        print(f"changed_files: {session.state['changed_files']}")
        print(f"Events: {len(events)}")
        for e in events:
            author = e.author
            parts_info = []
            if e.content and e.content.parts:
                for p in e.content.parts:
                    if p.text:
                        parts_info.append(f"text({len(p.text)} chars)")
                    if p.function_call:
                        parts_info.append(f"call({p.function_call.name})")
                    if p.function_response:
                        parts_info.append(f"response({p.function_response.name})")
            delta = dict(e.actions.state_delta) if e.actions.state_delta else {}
            print(f"  [{author}] {', '.join(parts_info)}  delta_keys={list(delta.keys())}")


@pytest.mark.asyncio
async def test_changed_files_populated_by_fetch():
    """fetch_pr_diff populates changed_files in state with rich data
    (functions_touched populated by inline diff parsing)."""
    from pr_docs_reviewer.tools import fetch_pr_diff

    def fetch_only_callback(*, callback_context: CallbackContext, llm_request: LlmRequest) -> LlmResponse:
        """Simulate pr_analyzer that calls fetch_pr_diff then returns text."""
        agent_name = callback_context.agent_name

        if agent_name == "pr_analyzer":
            # Check if this is the first call (no function response yet)
            has_function_response = any(
                p.function_response
                for c in (llm_request.contents or [])
                for p in (c.parts or [])
                if p.function_response
            )

            if not has_function_response:
                return LlmResponse(
                    content=types.Content(
                        role="model",
                        parts=[types.Part(
                            function_call=types.FunctionCall(
                                name="fetch_pr_diff",
                                args={"pr_url": "https://github.com/owner/repo/pull/1"},
                            )
                        )],
                    ),
                )
            else:
                # After getting tool result, return final text
                return LlmResponse(
                    content=types.Content(
                        role="model",
                        parts=[types.Part(text='{"pr_number": 1, "summary": "test"}')],
                    ),
                )

        # For code_mapper
        return LlmResponse(
            content=types.Content(
                role="model",
                parts=[types.Part(text=f"[{agent_name}] done")],
            ),
        )

    pr_analyzer = Agent(
        name="pr_analyzer",
        model="gemini-2.0-flash",
        instruction="Analyze the PR.",
        tools=[fetch_pr_diff],
        output_key="pr_summary",
        before_model_callback=fetch_only_callback,
    )

    code_mapper = Agent(
        name="code_mapper",
        model="gemini-2.0-flash",
        instruction="Changed files: {changed_files}",
        tools=[],
        output_key="code_analysis",
        before_model_callback=fetch_only_callback,
    )

    pipeline = SequentialAgent(
        name="test_pipeline",
        sub_agents=[pr_analyzer, code_mapper],
    )

    async with InMemoryRunner(agent=pipeline, app_name="test") as runner:
        session = await runner.session_service.create_session(
            app_name="test", user_id="u1", session_id="s2",
        )

        set_backend(_make_mock_backend())
        async for event in runner.run_async(
            user_id="u1",
            session_id="s2",
            new_message=types.Content(
                role="user",
                parts=[types.Part(text="Review https://github.com/owner/repo/pull/1")],
            ),
        ):
            pass

        session = await runner.session_service.get_session(
            app_name="test", user_id="u1", session_id="s2",
        )

        # Key assertion: changed_files populated with rich data from inline parsing
        assert "changed_files" in session.state, (
            f"changed_files missing from state! Keys: {list(session.state.keys())}"
        )

        changed = session.state["changed_files"]
        # _FAKE_DIFF_TEXT only has src/client.py — the other file (src/utils.py)
        # is in _FAKE_PR_FILES but not in the diff; parsed result only includes
        # files actually in the diff
        assert len(changed) >= 1
        assert changed[0]["path"] == "src/client.py"
        # functions_touched should be populated (not empty) from inline parsing
        assert len(changed[0]["functions_touched"]) >= 1


# ---------------------------------------------------------------------------
# Doc Applier integration tests
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_doc_applier_skips_when_auto_apply_false():
    """When auto_apply is not set, doc_applier outputs a summary without calling tools."""
    from pr_docs_reviewer.tools.apply_doc_updates import apply_doc_updates

    def applier_callback(*, callback_context: CallbackContext, llm_request: LlmRequest) -> LlmResponse:
        agent_name = callback_context.agent_name
        if agent_name == "doc_applier":
            # The LLM should NOT call apply_doc_updates — just return a summary
            return LlmResponse(
                content=types.Content(
                    role="model",
                    parts=[types.Part(text=json.dumps({
                        "summary": "Would update docs/api.md with retry info.",
                        "applied": False,
                    }))],
                ),
            )
        return LlmResponse(
            content=types.Content(
                role="model",
                parts=[types.Part(text=f"[{agent_name}] done")],
            ),
        )

    doc_applier = Agent(
        name="doc_applier",
        model="gemini-2.0-flash",
        instruction="Check auto_apply: {auto_apply?}. Suggestions: {doc_suggestions}",
        tools=[apply_doc_updates],
        output_key="apply_result",
        before_model_callback=applier_callback,
    )

    pipeline = SequentialAgent(
        name="test_applier_pipeline",
        sub_agents=[doc_applier],
    )

    mock_backend = _make_mock_backend()
    set_backend(mock_backend)

    async with InMemoryRunner(agent=pipeline, app_name="test") as runner:
        session = await runner.session_service.create_session(
            app_name="test",
            user_id="u1",
            session_id="s3",
            state={
                "doc_suggestions": '[{"doc_path": "docs/api.md", "suggested_text": "new text"}]',
                "pr_url": "https://github.com/owner/repo/pull/1",
            },
        )

        async for event in runner.run_async(
            user_id="u1",
            session_id="s3",
            new_message=types.Content(
                role="user",
                parts=[types.Part(text="Apply suggestions")],
            ),
        ):
            pass

        session = await runner.session_service.get_session(
            app_name="test", user_id="u1", session_id="s3",
        )

        # doc_applier should have run and produced apply_result
        assert "apply_result" in session.state
        # Backend write methods should NOT have been called
        mock_backend.create_branch.assert_not_called()
        mock_backend.write_file.assert_not_called()
        mock_backend.create_pull_request.assert_not_called()


@pytest.mark.asyncio
async def test_doc_applier_calls_tool_when_auto_apply_true():
    """When auto_apply is true, doc_applier calls apply_doc_updates."""
    from pr_docs_reviewer.tools.apply_doc_updates import apply_doc_updates

    call_count = {"n": 0}

    def applier_callback(*, callback_context: CallbackContext, llm_request: LlmRequest) -> LlmResponse:
        agent_name = callback_context.agent_name
        if agent_name == "doc_applier":
            if call_count["n"] == 0:
                call_count["n"] += 1
                # First call: invoke the tool
                return LlmResponse(
                    content=types.Content(
                        role="model",
                        parts=[types.Part(
                            function_call=types.FunctionCall(
                                name="apply_doc_updates",
                                args={},
                            )
                        )],
                    ),
                )
            else:
                # Second call: after tool returns, produce final text
                call_count["n"] += 1
                return LlmResponse(
                    content=types.Content(
                        role="model",
                        parts=[types.Part(text="Doc PR created successfully.")],
                    ),
                )
        return LlmResponse(
            content=types.Content(
                role="model",
                parts=[types.Part(text=f"[{agent_name}] done")],
            ),
        )

    doc_applier = Agent(
        name="doc_applier",
        model="gemini-2.0-flash",
        instruction="auto_apply: {auto_apply?}. Suggestions: {doc_suggestions}",
        tools=[apply_doc_updates],
        output_key="apply_result",
        before_model_callback=applier_callback,
    )

    pipeline = SequentialAgent(
        name="test_applier_pipeline",
        sub_agents=[doc_applier],
    )

    mock_backend = _make_mock_backend()
    mock_backend.get_pr_head_ref.return_value = ("feature/add-retry", "abc123")
    mock_backend.create_branch.return_value = None
    mock_backend.read_file.return_value = "# API\n\nThe timeout defaults to 30 seconds.\n"
    mock_backend.write_file.return_value = None
    mock_backend.create_pull_request.return_value = {
        "number": 99,
        "html_url": "https://github.com/owner/repo/pull/99",
    }
    set_backend(mock_backend)

    suggestions = json.dumps([{
        "doc_path": "docs/api.md",
        "changes_summary": "Added max_retries parameter documentation.",
        "original_content": "# API\n\nThe timeout defaults to 30 seconds.\n",
        "suggested_content": "# API\n\nThe timeout defaults to 30 seconds. You can also set max_retries.\n",
    }])

    async with InMemoryRunner(agent=pipeline, app_name="test") as runner:
        session = await runner.session_service.create_session(
            app_name="test",
            user_id="u1",
            session_id="s4",
            state={
                "auto_apply": "true",
                "doc_suggestions": suggestions,
                "repo": "owner/repo",
                "pr_number": 1,
                "pr_url": "https://github.com/owner/repo/pull/1",
            },
        )

        async for event in runner.run_async(
            user_id="u1",
            session_id="s4",
            new_message=types.Content(
                role="user",
                parts=[types.Part(text="Apply suggestions")],
            ),
        ):
            pass

        session = await runner.session_service.get_session(
            app_name="test", user_id="u1", session_id="s4",
        )

        # apply_doc_updates should have been invoked (backend calls made)
        mock_backend.get_pr_head_ref.assert_called_once()
        mock_backend.create_branch.assert_called_once()
        mock_backend.write_file.assert_called_once()
        mock_backend.create_pull_request.assert_called_once()

        # apply_result should be in state
        assert "apply_result" in session.state
