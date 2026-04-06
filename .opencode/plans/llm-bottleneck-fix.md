# LLM Bottleneck Fix — Implementation Plan

## Problem
`fetch_pr_diff` returns the entire raw diff text to the LLM, which then relays it as an argument to `parse_changed_files`. For large PRs, the diff travels through LLM context twice (~10,000+ tokens wasted).

## Solution
Merge `parse_changed_files` into `fetch_pr_diff`. After fetching raw diff from GitHub, parse it with `PatchSet` inline. Return only a compact structured summary — never return raw diff text.

## Files to Change (7 files)

### 1. `pr_docs_reviewer/tools/fetch_pr_diff.py` — Merge parsing logic in
- Import `PatchSet` from `unidiff`, `re`
- Move `_extract_function_name` and `_extract_definition_name` here (renamed to `extract_function_name` / `extract_definition_name` — public, no underscore)
- Add internal `_parse_diff(diff_text) -> list[dict]` that runs PatchSet + function extraction
- In `fetch_pr_diff()`: after fetching raw diff, call `_parse_diff()` to get rich structured data
- **Remove `"diff"` from return dict** — replace with structured `files_changed` (list of dicts with path, change_type, functions_touched, additions, deletions, hunk_ranges)
- Rename return keys: `additions`/`deletions` → `total_additions`/`total_deletions`; `files_changed` is now list[dict] not list[str]
- Fallback: if PatchSet fails, use GitHub files API data (basic list)
- Write rich `changed_files` to state (with functions_touched populated)

**New return dict shape:**
```python
{
    "status": "success",
    "pr_number": 42,
    "pr_title": "Add retry logic",
    "pr_body": "...",
    "files_changed": [
        {
            "path": "src/client.py",
            "change_type": "modified",
            "functions_touched": ["connect", "_retry"],
            "additions": 10,
            "deletions": 2,
            "hunk_ranges": [{"start": 10, "end": 18}]
        }
    ],
    "total_additions": 15,
    "total_deletions": 2
}
```

### 2. `pr_docs_reviewer/tools/parse_changed_files.py` — DELETE
- All logic moved to `fetch_pr_diff.py`
- Helper functions now live in `fetch_pr_diff.py` as public functions

### 3. `pr_docs_reviewer/tools/__init__.py` — Remove export
- Remove `from .parse_changed_files import parse_changed_files`
- Remove from `__all__`

### 4. `pr_docs_reviewer/agent.py` — Simplify pr_analyzer
- Remove `parse_changed_files` from imports
- Remove from `pr_analyzer.tools`
- Simplify `PR_ANALYZER_INSTRUCTION`:
  - Step 1: Call `fetch_pr_diff` (returns structured summary, no raw diff)
  - Step 2: Synthesize into JSON summary
  - No step for calling `parse_changed_files` (doesn't exist anymore)

### 5. `tests/tools/test_fetch_pr_diff.py` — Add merged parsing tests
- Update `test_successful_fetch`: `result` no longer has `"diff"` key; `files_changed` is list[dict] not list[str]; has `total_additions`/`total_deletions`
- Update `test_writes_changed_files_to_state`: `changed_files` now has `functions_touched` populated (not empty `[]`) when diff is parseable
- Add: `test_files_changed_has_functions_touched` — verify parsed diff populates functions
- Add: `test_files_changed_has_hunk_ranges` — verify hunk_ranges present
- Add: `test_no_raw_diff_in_result` — assert `"diff"` not in result
- Add: `test_fallback_on_parse_failure` — mock PatchSet to fail, verify basic file list returned
- Need to provide a real parseable diff in mock `diff_resp.text` for parsing tests

### 6. `tests/tools/test_parse_changed_files.py` — Refactor
- `TestParseChangedFiles` class: DELETE entirely (tool function removed)
- `TestExtractFunctionName`: update import from `pr_docs_reviewer.tools.fetch_pr_diff` → `extract_function_name`
- `TestExtractDefinitionName`: update import from `pr_docs_reviewer.tools.fetch_pr_diff` → `extract_definition_name`

### 7. `tests/test_pipeline_state.py` — Simplify mock callbacks
- `_make_mock_callback()`: pr_analyzer now needs 2 LLM calls (not 3):
  - Call 0: function_call `fetch_pr_diff`
  - Call 1: return final text (skip parse_changed_files call entirely)
- `test_state_propagation_pr_analyzer_to_research_phase`:
  - Remove `parse_changed_files` from imports and tools list
  - Provide real parseable diff in `_FAKE_DIFF_TEXT` (already is parseable)
- `test_changed_files_available_even_without_parse_tool`:
  - Remove `parse_changed_files` from imports and tools list
  - Simplify title/description (the "without parse tool" concept no longer applies — rename to something like `test_changed_files_populated_by_fetch`)

## Execution Order
1. Write new `fetch_pr_diff.py`
2. Update `__init__.py`
3. Update `agent.py`
4. Update `test_fetch_pr_diff.py`
5. Refactor `test_parse_changed_files.py`
6. Simplify `test_pipeline_state.py`
7. Delete `parse_changed_files.py`
8. Run all tests
