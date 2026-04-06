# PR Documentation Reviewer — Multi-Agent System Design

## 1. Problem Statement

When code changes land in a pull request, documentation often lags behind. Engineers merge behavioral changes, new parameters, renamed functions, or removed features — and the docs silently go stale. Manual review is tedious and easy to skip.

This system automates the following workflow:

1. **Review** the changes in a PR.
2. **Find** documentation sections that are touched or invalidated by those changes.
3. **Assess** whether the docs actually need updating (filter out noise).
4. **Suggest** concrete documentation changes with rationale.

The system is built as a multi-agent pipeline on **Google ADK (Agent Development Kit)** for Python.

---

## 2. Why Google ADK

Google ADK provides purpose-built primitives for multi-agent orchestration that map directly to this problem:

| ADK Primitive | How We Use It |
|---|---|
| `SequentialAgent` | Deterministic pipeline flow — no LLM tokens wasted on routing decisions |
| `ParallelAgent` | Code analysis and doc search run concurrently, cutting wall-clock time |
| `LoopAgent` | Generator-critic refinement loop for doc suggestions with bounded iterations |
| `LlmAgent` | Each specialist agent with focused instructions and dedicated tools |
| `output_key` | Automatic state persistence — agent output flows to the next stage without glue code |
| `session.state` + `{key}` templating | Data passes between agents via shared state, injected directly into instructions |
| `FunctionTool` | Plain Python functions auto-wrapped as LLM-callable tools |

### Why Not a Single LLM Orchestrator?

An LLM-based coordinator (e.g., a root `LlmAgent` with `sub_agents` using `transfer_to_agent`) would spend tokens deciding "what to do next" on every run — even though the pipeline is always the same: analyze PR, research, assess, write, review. `SequentialAgent` gives us that deterministic flow at zero LLM cost, while each step still gets full LLM reasoning power for its specialized task.

---

## 3. Architecture Overview

```
                     SequentialAgent ("pr_docs_pipeline")
                                   |
              +--------------------+--------------------+
              |                    |                    |
         [Step 1]            [Step 2]             [Step 3]
       pr_analyzer        ParallelAgent         LoopAgent
        (LlmAgent)     ("research_phase")   ("refinement_loop")
              |               |                    |
              |         +-----+-----+        +-----+-----+
              |         |           |        |           |
              |    code_mapper  doc_finder  doc_writer  quality_reviewer
              |    (LlmAgent)  (LlmAgent)  (LlmAgent)    (LlmAgent)
              |
         Fetches PR diff,
         extracts structured
         change summary
```

### Pipeline Stages

| Stage | Agent(s) | ADK Type | Purpose |
|---|---|---|---|
| **1. Analyze** | `pr_analyzer` | `LlmAgent` | Fetch PR diff, parse into structured change list |
| **2. Research** | `code_mapper` + `doc_finder` | `ParallelAgent` wrapping two `LlmAgent`s | Deep code analysis and doc search run concurrently |
| **3. Refine** | `doc_writer` + `quality_reviewer` | `LoopAgent` wrapping two `LlmAgent`s | Write suggestions, critique, iterate (max 3 rounds) |

---

## 4. Data Flow

All inter-agent communication uses `session.state` via `output_key`. Each agent reads its predecessors' output through `{key}` template injection in its instructions.

```
User Input (PR URL)
       |
       v
  pr_analyzer
       |---> state["pr_summary"]       Structured change summary (JSON)
       |---> state["changed_files"]    List of {path, functions, change_type}
       |
       v
  +--- research_phase (parallel) ---+
  |                                 |
  |  code_mapper                    |  doc_finder
  |  reads: {changed_files}         |  reads: {pr_summary}, {changed_files}
  |  writes: state["code_analysis"] |  writes: state["relevant_doc_sections"]
  |                                 |
  +---------------------------------+
       |
       v
  +--- refinement_loop (max 3 iterations) ---+
  |                                           |
  |  doc_writer                               |
  |  reads: {code_analysis},                  |
  |         {relevant_doc_sections},          |
  |         {reviewer_feedback?}              |
  |  writes: state["doc_suggestions"]         |
  |                                           |
  |  quality_reviewer                         |
  |  reads: {doc_suggestions},                |
  |         {code_analysis},                  |
  |         {relevant_doc_sections}           |
  |  writes: state["reviewer_feedback"]       |
  |  escalate=True when quality is acceptable |
  +-------------------------------------------+
       |
       v
  Final Output: state["doc_suggestions"]
```

### State Key Reference

| Key | Written By | Read By | Content |
|---|---|---|---|
| `pr_summary` | `pr_analyzer` | `doc_finder` | Structured PR change summary |
| `changed_files` | `pr_analyzer` | `code_mapper`, `doc_finder` | List of file paths, functions touched, change types |
| `code_analysis` | `code_mapper` | `relevance_assessor` (within `doc_writer`), `doc_writer`, `quality_reviewer` | Semantic descriptions of what changed and user-facing impact |
| `relevant_doc_sections` | `doc_finder` | `doc_writer`, `quality_reviewer` | Doc file paths, sections, snippets, and why they need updating |
| `doc_suggestions` | `doc_writer` | `quality_reviewer`, final output | Concrete suggested doc changes with rationale |
| `reviewer_feedback` | `quality_reviewer` | `doc_writer` (next iteration) | Specific critique and improvement instructions |

---

## 5. Agent Specifications

### 5.1 `pr_analyzer`

**Type:** `LlmAgent`
**Role:** Entry point. Fetches the PR diff and produces a structured, machine-readable summary of all changes.

**Reasoning:** We need a clean, structured representation of what changed before any downstream agent can work. Raw diffs are noisy — this agent distills them into a format that other agents can reason about efficiently without re-parsing the diff themselves.

**Tools:**
- `fetch_pr_diff` — calls GitHub API
- `parse_changed_files` — extracts structured file/function info from the diff

**Instructions (summary):**
> Fetch the PR diff using the provided URL. Parse the diff to identify all changed files. For each file, determine:
> - The type of change (added, modified, deleted, renamed)
> - Which functions, classes, or methods were touched
> - A brief description of what changed in each hunk
>
> Output a structured JSON summary. Focus on changes that could affect public API surface, user-facing behavior, configuration options, or documented features.

**Output key:** `pr_summary`
**Also writes to state:** `changed_files` (via tool side-effect in `parse_changed_files`)

**Output schema:**
```json
{
  "pr_number": 123,
  "pr_title": "Add retry logic to HTTP client",
  "files": [
    {
      "path": "src/http/client.py",
      "change_type": "modified",
      "functions_touched": ["Client.request", "Client.__init__"],
      "summary": "Added max_retries parameter to Client.__init__ and retry loop in Client.request"
    }
  ],
  "overall_summary": "Adds configurable retry logic to the HTTP client with exponential backoff"
}
```

---

### 5.2 `code_mapper`

**Type:** `LlmAgent` (runs within `ParallelAgent`)
**Role:** Reads the actual source code around changes to understand *what changed semantically* — not just which lines moved.

**Reasoning:** The PR diff tells you *what* lines changed, but not *why it matters*. A function signature change might add a new required parameter (breaking change, definitely needs doc update) or just rename an internal variable (irrelevant). The code mapper bridges this gap by reading the full code context and classifying the user-facing impact.

**Tools:**
- `read_file_contents` — reads full file from the repo at the PR's head ref
- `get_function_signatures` — extracts function/class signatures with docstrings

**Instructions (summary):**
> For each file in {changed_files}, read the actual source code and the function signatures. Compare against the change descriptions in the PR summary. For each changed function or class, determine:
> - What changed semantically (new parameter, changed return type, different behavior, etc.)
> - Whether this is user-facing (affects public API, CLI, config) or internal-only
> - The severity: breaking change, new feature, behavioral change, or cosmetic
>
> Focus on changes that documentation consumers would care about. Internal refactoring with no external effect should be explicitly marked as "no doc impact".

**Output key:** `code_analysis`

**Output schema:**
```json
[
  {
    "file": "src/http/client.py",
    "function": "Client.__init__",
    "change_type": "new_parameter",
    "description": "Added 'max_retries' parameter (int, default=3) to control retry behavior",
    "user_facing_impact": "high",
    "doc_relevance": "Users need to know about this new configuration option"
  },
  {
    "file": "src/http/client.py",
    "function": "Client._parse_headers",
    "change_type": "internal_refactor",
    "description": "Extracted header parsing into a private method",
    "user_facing_impact": "none",
    "doc_relevance": "No doc update needed"
  }
]
```

---

### 5.3 `doc_finder`

**Type:** `LlmAgent` (runs within `ParallelAgent`)
**Role:** Searches the documentation for pages that reference the changed code, then assesses which matches are actually relevant.

**Reasoning:** We initially considered a separate `relevance_assessor` agent, but merged the relevance check into the doc finder for two reasons: (1) the doc finder already has all the context needed to judge relevance — it knows what it searched for and why — and (2) adding a separate agent for a filtering task adds latency and token cost for a task that is better done inline. The doc finder uses multiple search strategies (keyword, file reference) to cast a wide net, then filters down.

**Tools:**
- `search_docs_by_keyword` — searches docs for function names, class names, concepts
- `search_docs_by_file_reference` — searches docs for references to specific source file paths
- `read_doc_file` — reads a doc file to verify the match is real

**Instructions (summary):**
> Given the PR summary in {pr_summary} and changed files in {changed_files}:
>
> 1. Extract search terms: function names, class names, module names, and key concepts from the changes.
> 2. Search the docs using both keyword search and file reference search.
> 3. For each match, read the doc file to verify the reference is substantive (not just a passing mention in a changelog or unrelated example).
> 4. Filter out matches where the doc section would not need updating based on the nature of the code change.
> 5. Rank remaining matches by update urgency (breaking changes > new features > behavioral changes).
>
> Only return doc sections that genuinely need attention. Do not include tangential mentions.

**Output key:** `relevant_doc_sections`

**Output schema:**
```json
[
  {
    "doc_path": "docs/http-client.md",
    "section_heading": "## Configuration",
    "line_range": [45, 72],
    "current_content_snippet": "The Client accepts `timeout` and `base_url` parameters...",
    "reason_for_update": "Missing documentation for new 'max_retries' parameter",
    "urgency": "high"
  }
]
```

---

### 5.4 `doc_writer`

**Type:** `LlmAgent` (runs within `LoopAgent`)
**Role:** Writes concrete documentation update suggestions.

**Reasoning:** This is the generative core of the system. It takes structured inputs (what changed, which docs are affected) and produces actionable suggestions. Separating this from the analysis/search phases means the writer has clean, pre-filtered inputs and can focus entirely on producing high-quality prose. The `{reviewer_feedback?}` template (with `?` for optional) allows the same agent to work on first drafts and revisions.

**Tools:**
- `read_doc_file` — reads full doc content for context around the snippet

**Instructions (summary):**
> Given code changes from {code_analysis} and relevant doc sections from {relevant_doc_sections}:
>
> For each relevant doc section, write a concrete suggestion:
> - Read the full doc file for context (tone, structure, terminology)
> - Determine the type of change needed: update existing text, add new section, add new example, update code sample, add warning/note
> - Write the suggested text, matching the existing doc's style and tone
> - Provide a clear rationale linking the suggestion to the code change
>
> If {reviewer_feedback} is present, this is a revision. Address every point in the feedback specifically.
>
> Output format: a list of suggestions, each with the doc path, section, change type, the suggested new text, and the rationale.

**Output key:** `doc_suggestions`

**Output schema:**
```json
[
  {
    "doc_path": "docs/http-client.md",
    "section": "## Configuration",
    "change_type": "add_parameter_docs",
    "current_text": "The Client accepts `timeout` and `base_url` parameters.",
    "suggested_text": "The Client accepts `timeout`, `base_url`, and `max_retries` parameters.\n\n- `max_retries` (int, default=3): Number of retry attempts for failed requests. Uses exponential backoff between retries.",
    "rationale": "PR #123 added a new 'max_retries' parameter to Client.__init__. Users need to know about this configuration option."
  }
]
```

---

### 5.5 `quality_reviewer`

**Type:** `LlmAgent` (runs within `LoopAgent`, after `doc_writer`)
**Role:** Evaluates the doc suggestions and either approves them or provides specific improvement feedback.

**Reasoning:** LLM-generated text benefits significantly from a review pass. The Generator-Critic pattern is one of ADK's documented multi-agent patterns for exactly this purpose. A separate reviewer agent avoids the problem of self-review bias (where the same agent evaluates its own work). The reviewer has strict criteria and must justify its decision, which produces better feedback than a vague "try again."

**Tools:** None — pure reasoning over state. No tools needed because it already has all the context in state.

**Instructions (summary):**
> You are a documentation quality reviewer. Evaluate the suggestions in {doc_suggestions} against the code changes in {code_analysis} and the original doc sections in {relevant_doc_sections}.
>
> Evaluate each suggestion against these criteria:
>
> 1. **Accuracy**: Does the suggestion correctly reflect the code change? No hallucinated parameters, no wrong defaults, no incorrect behavior descriptions.
> 2. **Completeness**: Are all relevant doc sections addressed? Are there code changes with doc impact that were missed?
> 3. **Tone & Style**: Does the suggested text match the existing documentation's voice, terminology, and formatting conventions?
> 4. **Specificity**: Are suggestions concrete and actionable? Vague suggestions like "update the docs" are not acceptable.
> 5. **Conciseness**: Is the suggested text appropriately scoped, or does it over-explain?
>
> If ALL suggestions pass all criteria: approve by setting escalate=True.
> If ANY suggestion needs improvement: provide specific, actionable feedback for each issue. Do NOT rewrite the suggestions — that is the writer's job.

**Output key:** `reviewer_feedback`
**Escalation:** `escalate=True` when all suggestions pass quality criteria.

**Output schema (when not approving):**
```json
{
  "approved": false,
  "issues": [
    {
      "suggestion_index": 0,
      "criterion": "accuracy",
      "issue": "The default value for max_retries is stated as 5, but the code shows it defaults to 3",
      "fix_instruction": "Change the default value to 3"
    }
  ],
  "overall_feedback": "One factual error in the retry parameter default. Otherwise good."
}
```

**Output schema (when approving):**
```json
{
  "approved": true,
  "issues": [],
  "overall_feedback": "All suggestions are accurate, complete, and well-written."
}
```

---

## 6. Refinement Loop Mechanics

The `LoopAgent` wrapping `doc_writer` and `quality_reviewer` implements ADK's Generator-Critic pattern:

```
Iteration 1:
  doc_writer  -> state["doc_suggestions"]    (first draft)
  reviewer    -> state["reviewer_feedback"]  (critique or approve)
                 if approved: escalate=True -> loop exits

Iteration 2 (if not approved):
  doc_writer  -> reads {reviewer_feedback}, rewrites suggestions
  reviewer    -> evaluates revised suggestions

Iteration 3 (if still not approved):
  doc_writer  -> final attempt with accumulated feedback
  reviewer    -> must approve or loop exits at max_iterations
```

**Why max 3 iterations?**
- Iteration 1 produces a reasonable first draft in most cases
- Iteration 2 catches factual errors and style issues
- Iteration 3 is a safety net; if 2 rounds of feedback haven't resolved issues, more iterations are unlikely to help and will waste tokens
- ADK's `LoopAgent` terminates on either `escalate=True` OR reaching `max_iterations`, so the pipeline always completes

**Why not 1 iteration (no loop)?**
In testing LLM-generated documentation, the most common failure modes are: hallucinated parameter defaults, missing edge cases mentioned in the code, and tone mismatch with existing docs. A single review pass catches these at low cost (one extra LLM call).

---

## 7. Tool Specifications

All tools authenticate with GitHub via the `GITHUB_TOKEN` environment variable. The code and documentation live in the **same repository**.

### 7.1 `fetch_pr_diff`

```python
def fetch_pr_diff(pr_url: str) -> dict:
    """
    Fetches the diff/patch content of a GitHub Pull Request.

    Args:
        pr_url: The full GitHub PR URL
                (e.g., "https://github.com/owner/repo/pull/123")

    Returns:
        dict with keys:
            - status: "success" or "error"
            - pr_number: int
            - pr_title: str
            - pr_body: str (PR description)
            - diff: str (unified diff content)
            - files_changed: list of filenames
            - additions: int (total lines added)
            - deletions: int (total lines removed)
    """
```

**Implementation notes:**
- Parse `owner`, `repo`, and `pr_number` from the URL using regex
- Use GitHub REST API: `GET /repos/{owner}/{repo}/pulls/{pr_number}` for metadata, `GET /repos/{owner}/{repo}/pulls/{pr_number}/files` for file-level changes
- Use `Accept: application/vnd.github.v3.diff` header for raw diff
- Auth via `Authorization: Bearer {GITHUB_TOKEN}` header
- Handle rate limiting with retry + backoff

### 7.2 `parse_changed_files`

```python
def parse_changed_files(diff: str) -> dict:
    """
    Parses a unified diff string and extracts structured change information.

    Args:
        diff: The unified diff content from a PR.

    Returns:
        dict with key "files" containing a list of dicts, each with:
            - path: str (file path)
            - change_type: "added" | "modified" | "deleted" | "renamed"
            - hunks: list of {start_line, end_line, content}
            - functions_touched: list of function/class names near changes
    """
```

**Implementation notes:**
- Use the `unidiff` Python library for robust diff parsing
- Extract function names by scanning context lines (lines starting with `@@`) which Git includes in unified diff headers
- Additionally scan for `def `, `class `, `function `, `func ` patterns in the surrounding context lines of each hunk
- Write the extracted file list to `session.state["changed_files"]` as a side effect via the `tool_context` parameter

### 7.3 `read_file_contents`

```python
def read_file_contents(
    file_path: str,
    repo: str,
    ref: str = "HEAD"
) -> dict:
    """
    Reads the contents of a file from a GitHub repository at a specific ref.

    Args:
        file_path: Path to the file within the repo (e.g., "src/auth/login.py")
        repo: Repository in "owner/repo" format (e.g., "google/adk-python")
        ref: Git ref — branch name, tag, or commit SHA. Defaults to "HEAD".

    Returns:
        dict with keys:
            - status: "success" or "error"
            - content: str (decoded file contents)
            - size_bytes: int
            - error_message: str (only if status is "error")
    """
```

**Implementation notes:**
- GitHub Contents API: `GET /repos/{owner}/{repo}/contents/{path}?ref={ref}`
- Response body contains base64-encoded content; decode it
- Handle files > 1MB by falling back to the Git Blobs API
- Return a clear error message if the file is not found (404)

### 7.4 `get_function_signatures`

```python
def get_function_signatures(
    file_path: str,
    repo: str,
    ref: str = "HEAD"
) -> dict:
    """
    Extracts function and class signatures from a source file.

    Args:
        file_path: Path to the file within the repo.
        repo: Repository in "owner/repo" format.
        ref: Git ref to read from.

    Returns:
        dict with key "signatures" containing a list of:
            - name: str (function/class name)
            - type: "function" | "class" | "method"
            - signature: str (full signature line including params)
            - line_number: int
            - docstring_summary: str or null
    """
```

**Implementation notes:**
- Fetch the file using `read_file_contents` internally
- For Python files (`.py`): use `ast.parse()` to walk the AST and extract `FunctionDef`, `AsyncFunctionDef`, and `ClassDef` nodes with their full signatures and first-line docstrings
- For other languages: fall back to regex-based extraction (less precise but functional)
- Include parameter names, types (if annotated), and default values in the signature string

### 7.5 `search_docs_by_keyword`

```python
def search_docs_by_keyword(
    keywords: list[str],
    docs_path: str = "docs/"
) -> dict:
    """
    Searches the repository's docs directory for files containing
    any of the given keywords.

    Args:
        keywords: Search terms — function names, class names, concepts.
        docs_path: Subdirectory to search within. Defaults to "docs/".

    Returns:
        dict with key "results" containing a list of:
            - file_path: str
            - matches: list of {keyword, line_number, context_line}
            - match_count: int
    """
```

**Implementation notes:**
- Uses GitHub Code Search API: `GET /search/code?q={keyword}+repo:{owner}/{repo}+path:{docs_path}`
- Batches multiple keywords into a single query where possible (OR semantics)
- Handles rate limiting (30 requests/minute for code search)
- Filters results to only include markdown/rst/txt doc files
- Since code and docs are in the same repo, the `repo` parameter is inferred from the PR URL stored in state

### 7.6 `search_docs_by_file_reference`

```python
def search_docs_by_file_reference(
    source_file_paths: list[str],
    docs_path: str = "docs/"
) -> dict:
    """
    Searches docs for references to specific source file paths or
    their derived identifiers (module names, class names).

    Args:
        source_file_paths: Source code file paths that changed in the PR.
        docs_path: Subdirectory to search within.

    Returns:
        dict with key "results" containing a list of:
            - doc_file_path: str
            - referenced_source: str (which source path it references)
            - reference_context: str (the line containing the reference)
            - line_number: int
    """
```

**Implementation notes:**
- Derives multiple search terms from each file path:
  - Full path: `src/agents/llm_agent.py`
  - Module name: `llm_agent`
  - Class-case: `LlmAgent` (snake_case to PascalCase conversion)
  - Kebab-case: `llm-agent` (common in doc URLs and markdown filenames)
- Searches for each derived term using the same code search API
- Deduplicates results across search terms

### 7.7 `read_doc_file`

```python
def read_doc_file(
    file_path: str,
    ref: str = "main"
) -> dict:
    """
    Reads the full content of a documentation file and parses its structure.

    Args:
        file_path: Path to the doc file (e.g., "docs/agents/llm-agents.md")
        ref: Git ref to read from. Defaults to "main".

    Returns:
        dict with keys:
            - status: "success" or "error"
            - content: str (full file content)
            - sections: list of {heading, level, start_line, end_line}
            - error_message: str (only if status is "error")
    """
```

**Implementation notes:**
- Uses `read_file_contents` internally for the file fetch
- Parses markdown headings via regex: `^(#{1,6})\s+(.+)$`
- Computes `end_line` for each section as the line before the next heading of equal or higher level (or EOF)
- The `sections` list gives the writer agent a table of contents so it can reference specific sections without reading the entire file

---

## 8. ADK Implementation Skeleton

```python
from google.adk.agents import Agent, SequentialAgent, ParallelAgent, LoopAgent

# ---------- Step 1: PR Analysis ----------

pr_analyzer = Agent(
    name="pr_analyzer",
    model="gemini-2.0-flash",  # Fast model — this is structured extraction, not creative
    instruction="""You analyze GitHub Pull Requests.

Given a PR URL from the user, use the fetch_pr_diff tool to get the diff,
then use parse_changed_files to extract structured change information.

Focus on changes that affect:
- Public API surface (new/changed/removed functions, parameters, return types)
- User-facing behavior (different outputs, new error cases, performance changes)
- Configuration options (new settings, changed defaults)
- Documented features (anything a user reading the docs would care about)

Ignore:
- Internal refactoring with no external impact
- Test-only changes
- CI/CD configuration changes

Output a structured JSON summary of all meaningful changes.""",
    tools=[fetch_pr_diff, parse_changed_files],
    output_key="pr_summary",
)


# ---------- Step 2: Parallel Research ----------

code_mapper = Agent(
    name="code_mapper",
    model="gemini-2.0-flash",  # Fast model — reading and classifying code
    instruction="""You are a code analyst. Your job is to understand the semantic
meaning of code changes, not just the textual diff.

Given the changed files in {changed_files}, for each file:
1. Use read_file_contents to read the current version of the file.
2. Use get_function_signatures to extract all function/class signatures.
3. For each changed function/class, determine:
   - What changed semantically (new param, changed behavior, etc.)
   - Whether this is user-facing or internal-only
   - The severity: breaking_change, new_feature, behavioral_change, cosmetic

Mark internal refactoring explicitly as "no doc impact" so downstream agents
can skip it.

Output a JSON list of change analyses.""",
    tools=[read_file_contents, get_function_signatures],
    output_key="code_analysis",
)

doc_finder = Agent(
    name="doc_finder",
    model="gemini-2.0-flash",  # Fast model — search and filter
    instruction="""You find documentation that needs updating based on code changes.

Given {pr_summary} and {changed_files}:

1. Extract search terms from the changes: function names, class names,
   module names, configuration keys, and key concepts.
2. Use search_docs_by_keyword to find docs mentioning these terms.
3. Use search_docs_by_file_reference to find docs referencing changed files.
4. For each match, use read_doc_file to verify it is a substantive reference
   (not just a changelog entry or unrelated example).
5. Filter out tangential mentions. Only keep doc sections that would need
   updating given the nature of the code changes.
6. Rank by urgency: breaking changes > new features > behavioral changes.

Output a JSON list of relevant doc sections with reasons for updating.""",
    tools=[search_docs_by_keyword, search_docs_by_file_reference, read_doc_file],
    output_key="relevant_doc_sections",
)

research_phase = ParallelAgent(
    name="research_phase",
    sub_agents=[code_mapper, doc_finder],
)


# ---------- Step 3: Refinement Loop ----------

doc_writer = Agent(
    name="doc_writer",
    model="gemini-2.0-flash",  # Configurable — may benefit from a stronger model
    instruction="""You write documentation update suggestions.

Given:
- Code changes: {code_analysis}
- Doc sections needing updates: {relevant_doc_sections}
- Previous review feedback (if any): {reviewer_feedback?}

For each relevant doc section:
1. Use read_doc_file to get the full context around the section.
2. Match the existing doc's tone, terminology, and formatting.
3. Determine the change type: update_text, add_section, add_example,
   update_code_sample, add_note, add_warning.
4. Write the suggested new/replacement text.
5. Provide a rationale linking the suggestion to a specific code change.

If reviewer_feedback is present, this is a revision. Address every point
in the feedback specifically. Do not ignore any feedback item.

Output a JSON list of concrete suggestions.""",
    tools=[read_doc_file],
    output_key="doc_suggestions",
)

quality_reviewer = Agent(
    name="quality_reviewer",
    model="gemini-2.0-flash",  # Configurable — may benefit from a stronger model
    instruction="""You review documentation update suggestions for quality.

Evaluate {doc_suggestions} against {code_analysis} and {relevant_doc_sections}.

Criteria:
1. ACCURACY: Do suggestions correctly reflect code changes? Check parameter
   names, default values, types, and behavioral descriptions against the
   code analysis. Flag any hallucinated details.
2. COMPLETENESS: Are all relevant doc sections addressed? Are there code
   changes marked as user-facing in the code analysis that have no
   corresponding doc suggestion?
3. TONE & STYLE: Does suggested text match the existing doc's voice,
   formatting conventions, and terminology?
4. SPECIFICITY: Are suggestions concrete? Vague suggestions like "update
   the docs to reflect changes" are NOT acceptable.
5. CONCISENESS: Is the suggested text appropriately scoped?

If ALL suggestions pass ALL criteria: respond with {"approved": true} and
set escalate to exit the review loop.

If ANY suggestion has issues: provide specific, actionable feedback for each
problem. Do NOT rewrite the suggestions yourself — that is the writer's job.
Be precise about what is wrong and how to fix it.""",
    output_key="reviewer_feedback",
)

refinement_loop = LoopAgent(
    name="refinement_loop",
    max_iterations=3,
    sub_agents=[doc_writer, quality_reviewer],
)


# ---------- Top-Level Pipeline ----------

pipeline = SequentialAgent(
    name="pr_docs_pipeline",
    sub_agents=[pr_analyzer, research_phase, refinement_loop],
)
```

---

## 9. Model Selection Strategy

Models are intentionally set to a placeholder (`gemini-2.0-flash`) across all agents to be configured at deployment time. The reasoning for each agent's model needs:

| Agent | Recommended Tier | Reasoning |
|---|---|---|
| `pr_analyzer` | Flash (fast/cheap) | Structured extraction from a diff — not creative work |
| `code_mapper` | Flash (fast/cheap) | Reading code and classifying changes — pattern matching |
| `doc_finder` | Flash (fast/cheap) | Search orchestration and filtering — tool-heavy, light reasoning |
| `doc_writer` | Pro or Flash | Creative writing that must match existing doc tone. Benefits from a stronger model, but Flash may suffice for well-structured inputs |
| `quality_reviewer` | Pro or Flash | Critical evaluation — needs to catch subtle errors. A stronger model catches more issues but costs more |

The `doc_writer` and `quality_reviewer` are the best candidates for a more capable model since they do the most nuanced reasoning. The research/analysis agents are primarily tool-orchestrators where Flash performs well.

---

## 10. Error Handling Considerations

| Failure Mode | Mitigation |
|---|---|
| GitHub API rate limiting | Tools implement exponential backoff with jitter; surface clear error messages to the LLM so it can report the issue |
| Invalid PR URL | `pr_analyzer` instructions tell it to validate the URL format before calling tools; `fetch_pr_diff` returns a clear error dict |
| No docs found | `doc_finder` returns an empty list; `doc_writer` instructions handle this case by reporting "no documentation updates needed" |
| Large PR (100+ files) | `parse_changed_files` can be instructed to group by directory and summarize; alternatively, filter to only non-test, non-vendor files |
| Loop doesn't converge | `max_iterations=3` guarantees termination; the last iteration's suggestions are used as-is |
| File not found (deleted in PR) | `read_file_contents` returns a clear error; `code_mapper` instructions handle missing files by noting the deletion |

---

## 11. Constraints and Assumptions

1. **Same repository**: Code and documentation live in the same GitHub repo. The `repo` identifier is extracted once from the PR URL and reused across all tools.

2. **Authentication**: All GitHub API access uses the `GITHUB_TOKEN` environment variable. The token needs `repo` scope (or `public_repo` for public repos).

3. **Documentation format**: Assumes docs are primarily Markdown (`.md`). The `read_doc_file` tool parses Markdown headings. RST or other formats would need additional parsing logic.

4. **Language detection**: `get_function_signatures` uses `ast.parse` for Python files. Other languages fall back to regex-based extraction. Extending to TypeScript, Go, Java, etc. would require language-specific parsers.

5. **Context window**: Each agent's input (state values injected into instructions) must fit within the model's context window. For very large PRs, the `pr_analyzer` should produce a summarized output rather than including full diffs.

---

## 12. Future Extensions

- **GitHub Action integration**: Run the pipeline as a GitHub Action triggered on PR events, posting suggestions as PR review comments.
- **Human-in-the-loop**: Use ADK's callback mechanism to pause after `doc_suggestions` is produced, present the results to a human, and incorporate their feedback before finalizing.
- **Multi-repo support**: Extend tools to support a separate docs repository (different `owner/repo` for code vs. docs).
- **Incremental updates**: Cache previous analysis results to speed up re-runs when the PR is updated with new commits.
- **ADK 2.0 graph-based workflows**: When ADK 2.0's graph-based workflow primitives stabilize, the pipeline could be expressed as a directed graph with conditional edges (e.g., skip the loop if reviewer approves on first pass).
