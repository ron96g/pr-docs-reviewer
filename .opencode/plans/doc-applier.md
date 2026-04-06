# Doc Updater — Apply Suggestions as a PR

## Overview

Add an **optional Step 4** to the pipeline: after the refinement loop produces
approved `doc_suggestions`, apply them by creating a new branch, committing
the doc changes, and opening a PR against the **source PR's head branch**.

This step is **opt-in** — it only runs when the user sets `auto_apply=true`
in session state (or passes it as part of the input). The default behavior
remains review-only (suggest changes without applying them).

## Architecture Change

```
    SequentialAgent ("pr_docs_pipeline")
    ├── pr_analyzer (LlmAgent)
    ├── ParallelAgent("research_phase")
    │   ├── code_mapper (LlmAgent)
    │   └── doc_finder (LlmAgent)
    ├── LoopAgent("refinement_loop", max_iterations=3)
    │   ├── doc_writer (LlmAgent)
    │   └── quality_reviewer (LlmAgent)
    └── doc_applier (LlmAgent)           ← NEW (Step 4, opt-in)
```

`doc_applier` is a simple LlmAgent with a single tool (`apply_doc_updates`).
If `auto_apply` is not set in state, the agent's instruction tells it to
skip and output a summary message. If `auto_apply` is set, it calls
`apply_doc_updates` to create the branch, commit files, and open the PR.

**Why an LlmAgent and not a plain function?** The LLM can:
- Compose a good PR title and description from `doc_suggestions` + `pr_summary`
- Handle edge cases gracefully (e.g., no suggestions to apply, API errors)
- Format the output message for the user

## Backend Protocol Extension

The existing `RepoBackend` protocol (in `pr_docs_reviewer/tools/backend.py`) defines
five **read-only** methods used by the analysis/research tools:

| Method | Purpose |
|--------|---------|
| `get_pr_metadata()` | PR title, body, number, URL |
| `get_pr_diff()` | Raw unified diff text |
| `get_pr_files()` | List of changed files |
| `read_file(path, ref)` | File contents |
| `search_code(query, ...)` | Code search |

The apply step needs three **write** methods added to the protocol:

```python
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
```

### Why extend the protocol instead of calling GitHub API directly?

The doc applier must work in **both** modes:

| Operation | API mode (`GitHubAPIBackend`) | Local mode (`LocalBackend`) |
|-----------|-------------------------------|------------------------------|
| Get PR head ref | `GET /repos/.../pulls/{n}` → extract `head.ref`, `head.sha` | Read from `GITHUB_EVENT_PATH` payload: `pull_request.head.ref`, `pull_request.head.sha` |
| Create branch | `POST /repos/.../git/refs` | `git checkout -b {branch}` |
| Write file | `GET contents` (for blob SHA) + `PUT contents` | Write to filesystem + `git add` + `git commit` |
| Create PR | `POST /repos/.../pulls` | `gh pr create` (GitHub CLI, available in Actions) |

By keeping these on the backend, the `apply_doc_updates` tool remains
mode-agnostic — it calls `backend.create_branch(...)` and the right thing
happens regardless of whether we're using the API or local git.

## New Files

### 1. `pr_docs_reviewer/tools/apply_doc_updates.py` — The core tool

A single tool function that delegates all Git/GitHub work to the backend:

```python
def apply_doc_updates(tool_context: ToolContext) -> dict:
```

**Reads from state:**
- `doc_suggestions` — list of approved suggestions from the refinement loop
- `repo` — "owner/repo" string
- `pr_number` — int
- `pr_url` — str

**Workflow:**

```
1. backend = get_backend()

2. head_ref, head_sha = backend.get_pr_head_ref()
   → Gets the source PR's branch name and SHA

3. branch_name = f"docs/update-for-pr-{pr_number}"
   backend.create_branch(branch_name, head_sha)
   → Creates new branch from source PR's head

4. For each suggestion in doc_suggestions:
   a. Parse the suggestion's doc_path, current_text, suggested_text
   b. current_content = backend.read_file(doc_path, ref=branch_name)
      → Get the file's current content
   c. Apply the text replacement (see "Text Replacement Strategy" below)
   d. backend.write_file(
        path=doc_path,
        content=new_content,
        message=f"docs: update {doc_path} for PR #{pr_number}",
        branch=branch_name,
      )
      → Commits the change

5. result = backend.create_pull_request(
     title=f"docs: update documentation for PR #{pr_number}",
     body="<generated PR description with suggestion rationales>",
     head=branch_name,
     base=head_ref,   ← source PR's branch, NOT main
   )
   → Opens a PR targeting the source PR's branch
```

**Return value:**
```python
{
    "status": "success",
    "doc_pr_url": "https://github.com/owner/repo/pull/456",
    "doc_pr_number": 456,
    "branch": "docs/update-for-pr-123",
    "files_updated": ["docs/api.md", "docs/config.md"],
    "commit_count": 2
}
```

**Error handling:**
- Branch already exists (RuntimeError from `create_branch`) → delete and
  recreate, or append timestamp
- File not found during content read → skip that suggestion, log warning
- Text replacement fails (current_text not found in file) → skip, include
  in a `skipped_suggestions` list in the return value
- Any backend error → return partial result with error details

### 2. Backend implementations for write methods

#### `GitHubAPIBackend` additions (in `github_api_backend.py`)

```python
def get_pr_head_ref(self) -> tuple[str, str]:
    resp = github_get(f"/repos/{self._repo_path}/pulls/{self.pr_number}")
    data = resp.json()
    return data["head"]["ref"], data["head"]["sha"]

def create_branch(self, branch_name: str, sha: str) -> None:
    resp = github_post(
        f"/repos/{self._repo_path}/git/refs",
        json={"ref": f"refs/heads/{branch_name}", "sha": sha},
    )
    if resp.status_code == 422:
        raise RuntimeError(f"Branch {branch_name!r} already exists")
    resp.raise_for_status()

def write_file(self, path: str, content: str, message: str, branch: str) -> None:
    # Get current blob SHA (needed for updates)
    get_resp = github_get(
        f"/repos/{self._repo_path}/contents/{path}",
        params={"ref": branch},
        raw=True,
    )
    blob_sha = get_resp.json().get("sha") if get_resp.status_code == 200 else None

    body = {
        "message": message,
        "content": base64.b64encode(content.encode()).decode(),
        "branch": branch,
    }
    if blob_sha:
        body["sha"] = blob_sha

    resp = github_put(f"/repos/{self._repo_path}/contents/{path}", json=body)
    resp.raise_for_status()

def create_pull_request(self, title: str, body: str, head: str, base: str) -> dict:
    resp = github_post(
        f"/repos/{self._repo_path}/pulls",
        json={"title": title, "body": body, "head": head, "base": base},
    )
    resp.raise_for_status()
    data = resp.json()
    return {"number": data["number"], "html_url": data["html_url"]}
```

#### `LocalBackend` additions (in `local_backend.py`)

```python
def get_pr_head_ref(self) -> tuple[str, str]:
    # Read from the event payload (same file used by get_pr_metadata)
    event = self._load_event_payload()
    pr = event["pull_request"]
    return pr["head"]["ref"], pr["head"]["sha"]

def create_branch(self, branch_name: str, sha: str) -> None:
    subprocess.run(
        ["git", "checkout", "-b", branch_name, sha],
        cwd=self._repo_root,
        check=True,
        capture_output=True,
    )

def write_file(self, path: str, content: str, message: str, branch: str) -> None:
    file_path = self._repo_root / path
    file_path.parent.mkdir(parents=True, exist_ok=True)
    file_path.write_text(content, encoding="utf-8")
    subprocess.run(
        ["git", "add", path],
        cwd=self._repo_root, check=True, capture_output=True,
    )
    subprocess.run(
        ["git", "commit", "-m", message],
        cwd=self._repo_root, check=True, capture_output=True,
    )

def create_pull_request(self, title: str, body: str, head: str, base: str) -> dict:
    # Push the branch first
    subprocess.run(
        ["git", "push", "origin", head],
        cwd=self._repo_root, check=True, capture_output=True,
    )
    # Use GitHub CLI (available in all GitHub Actions runners)
    result = subprocess.run(
        ["gh", "pr", "create", "--title", title, "--body", body,
         "--head", head, "--base", base],
        cwd=self._repo_root, check=True, capture_output=True, text=True,
    )
    # gh pr create prints the PR URL to stdout
    pr_url = result.stdout.strip()
    # Extract PR number from URL
    pr_number = int(pr_url.rstrip("/").split("/")[-1])
    return {"number": pr_number, "html_url": pr_url}
```

### 3. Updates to `pr_docs_reviewer/tools/github_client.py`

Add two new functions following the same retry/backoff pattern as `github_get`:

```python
def github_post(path: str, *, json: dict, accept: str = ...) -> httpx.Response:
    """POST to GitHub API with retry on rate limits."""

def github_put(path: str, *, json: dict, accept: str = ...) -> httpx.Response:
    """PUT to GitHub API with retry on rate limits."""
```

Same `_headers()`, `_MAX_RETRIES`, `_BACKOFF_BASE`, rate-limit detection logic.
Only difference: `httpx.post`/`httpx.put` instead of `httpx.get`, `json=json`
instead of `params=params`.

These are called by `GitHubAPIBackend` only — the tool code never imports them
directly.

### 4. Updates to `pr_docs_reviewer/tools/__init__.py`

Add `apply_doc_updates` to imports and `__all__`.

### 5. Updates to `pr_docs_reviewer/tools/backend.py`

Add the four new write methods to the `RepoBackend` protocol.

### 6. Updates to `pr_docs_reviewer/agent.py`

Add the `doc_applier` LlmAgent:

```python
APPLIER_MODEL = os.environ.get("PR_DOCS_APPLIER_MODEL", _DEFAULT_MODEL)

DOC_APPLIER_INSTRUCTION = """\
You apply approved documentation suggestions by creating a PR.

Check if auto_apply is enabled: {auto_apply?}

If auto_apply is not set or is false:
  Output a summary of the approved suggestions. List each doc file and
  the changes that would be made. Do NOT call any tools.

If auto_apply is true:
  1. Call `apply_doc_updates` to create a branch, commit the doc changes,
     and open a PR against the source PR's branch.
  2. Report the result: the new PR URL, files updated, and any suggestions
     that were skipped.

The approved suggestions are: {doc_suggestions}
The source PR: {pr_url}
"""

doc_applier = Agent(
    name="doc_applier",
    model=APPLIER_MODEL,
    instruction=DOC_APPLIER_INSTRUCTION,
    tools=[apply_doc_updates],
    output_key="apply_result",
)
```

Update the pipeline:
```python
root_agent = SequentialAgent(
    name="pr_docs_pipeline",
    sub_agents=[pr_analyzer, research_phase, refinement_loop, doc_applier],
    ...
)
```

## Text Replacement Strategy

The trickiest part is reliably applying `current_text → suggested_text`
replacements. The `doc_suggestions` from `doc_writer` contain:

```json
{
    "doc_path": "docs/api.md",
    "section": "Client Configuration",
    "change_type": "update_text",
    "current_text": "The timeout defaults to 30 seconds.",
    "suggested_text": "The timeout defaults to 30 seconds. You can also configure max_retries (default: 3).",
    "rationale": "..."
}
```

**Approach — substring match with fallback:**

1. **Exact match**: `content.replace(current_text, suggested_text, 1)`
   Works when the LLM captured `current_text` verbatim.

2. **Normalized match**: Strip leading/trailing whitespace from both sides,
   normalize internal whitespace. Handles minor formatting differences.

3. **Section-based insertion** (for `add_section`, `add_note`, etc.):
   Find the section heading in the file, locate its end boundary (next
   heading of same or higher level), insert before it.

4. **Skip + report**: If none of the above work, skip the suggestion and
   include it in `skipped_suggestions` with the reason "could not locate
   target text in file".

This is a best-effort approach. The tool returns enough detail for the user
(or the LLM) to understand what was applied and what was skipped.

## State Keys Summary

| Key | Set by | Read by |
|-----|--------|---------|
| `auto_apply` | User (input) | `doc_applier` instruction |
| `doc_suggestions` | `doc_writer` | `doc_applier` instruction, `apply_doc_updates` tool |
| `repo` | `fetch_pr_diff` | `apply_doc_updates` tool |
| `pr_number` | `fetch_pr_diff` | `apply_doc_updates` tool |
| `pr_url` | `fetch_pr_diff` | `doc_applier` instruction |
| `apply_result` | `doc_applier` output_key | (final output) |

## Tests

All tests mock at the backend level using `set_backend()` — consistent with
the existing test strategy established during the backend refactor.

### Unit tests: `tests/tools/test_apply_doc_updates.py`

Uses `set_backend(mock_backend)` to inject a `MagicMock` implementing
the `RepoBackend` protocol. No direct patching of `github_get`/`github_post`.

1. **test_reads_suggestions_from_state** — verify tool reads `doc_suggestions`
2. **test_gets_pr_head_ref** — verify `backend.get_pr_head_ref()` is called
3. **test_creates_branch_from_pr_head** — verify `backend.create_branch()`
   receives the correct branch name and SHA
4. **test_writes_file_content** — verify `backend.write_file()` is called
   with the correct path, replaced content, message, and branch
5. **test_creates_pr_with_correct_base** — verify `backend.create_pull_request()`
   uses the source PR's head branch as base (not main)
6. **test_text_replacement_exact** — exact substring match works
7. **test_text_replacement_normalized** — whitespace-normalized match works
8. **test_skips_unfound_text** — when current_text not in file, suggestion
   is skipped and included in `skipped_suggestions`
9. **test_handles_branch_exists** — RuntimeError from `create_branch`
   handled gracefully
10. **test_handles_empty_suggestions** — no suggestions → returns early
    with `{"status": "success", "message": "No suggestions to apply"}`
11. **test_handles_backend_error** — backend exception returns error dict

### Unit tests: `tests/tools/test_github_api_backend.py` (extend existing)

Add tests for the four new write methods, mocking `github_get`/`github_post`/
`github_put` at the `pr_docs_reviewer.tools.github_api_backend` import path:

1. **test_get_pr_head_ref** — verify returns (branch_name, sha) tuple
2. **test_create_branch_success** — verify POST payload
3. **test_create_branch_already_exists** — 422 → RuntimeError
4. **test_write_file_update** — verify GET (blob SHA) + PUT sequence
5. **test_write_file_create_new** — verify PUT without sha when file is new
6. **test_create_pull_request** — verify POST payload and return dict

### Unit tests: `tests/tools/test_local_backend.py` (extend existing)

Add tests for the four new write methods using `tmp_path` fixtures and
mocked `subprocess.run`:

1. **test_get_pr_head_ref** — reads from event payload
2. **test_create_branch** — verify git checkout -b command
3. **test_write_file** — verify file written + git add + git commit
4. **test_write_file_creates_parent_dirs** — nested path creates dirs
5. **test_create_pull_request** — verify git push + gh pr create commands

### Unit tests: `tests/tools/test_github_client.py` (new)

1. **test_github_post_success** — mocked httpx POST, verify headers + body
2. **test_github_put_success** — mocked httpx PUT, verify headers + body
3. **test_github_post_rate_limit_retry** — 429 → retry with backoff
4. **test_github_put_rate_limit_retry** — 429 → retry with backoff

### Integration test updates: `tests/test_pipeline_state.py`

- Add a test that wires `doc_applier` into the pipeline with `auto_apply`
  set to `"false"` in state — verify it outputs a summary without calling
  `apply_doc_updates`
- Add a test with `auto_apply` = `"true"` — verify `apply_doc_updates`
  tool is called (mock backend via `set_backend()`)

## File Change Summary

| # | File | Action |
|---|------|--------|
| 1 | `pr_docs_reviewer/tools/backend.py` | Add 4 write methods to `RepoBackend` protocol |
| 2 | `pr_docs_reviewer/tools/github_api_backend.py` | Implement write methods using `github_post`/`github_put` |
| 3 | `pr_docs_reviewer/tools/local_backend.py` | Implement write methods using git/gh CLI |
| 4 | `pr_docs_reviewer/tools/github_client.py` | Add `github_post()` and `github_put()` |
| 5 | `pr_docs_reviewer/tools/apply_doc_updates.py` | **New** — the apply tool (uses `get_backend()`) |
| 6 | `pr_docs_reviewer/tools/__init__.py` | Add `apply_doc_updates` export |
| 7 | `pr_docs_reviewer/agent.py` | Add `doc_applier` agent, update pipeline |
| 8 | `tests/tools/test_apply_doc_updates.py` | **New** — 11 tests (mock backend via `set_backend`) |
| 9 | `tests/tools/test_github_api_backend.py` | Add 6 tests for write methods |
| 10 | `tests/tools/test_local_backend.py` | Add 5 tests for write methods |
| 11 | `tests/tools/test_github_client.py` | **New** — 4 tests for POST/PUT |
| 12 | `tests/test_pipeline_state.py` | Add 2 integration tests for apply step |

**Estimated: 2 new files, 8 modified files, ~28 new tests**
