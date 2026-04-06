"""
PR Documentation Reviewer — Multi-Agent Pipeline

Architecture:
    SequentialAgent("pr_docs_pipeline")
    ├── pr_analyzer (LlmAgent)
    ├── ParallelAgent("research_phase")
    │   ├── code_mapper (LlmAgent)
    │   └── doc_finder (LlmAgent)
    └── LoopAgent("refinement_loop", max_iterations=3)
        ├── doc_writer (LlmAgent)
        └── quality_reviewer (LlmAgent)

All inter-agent communication flows through session.state via output_key.
"""

import os

from google.adk.agents import Agent, SequentialAgent, ParallelAgent, LoopAgent
from google.adk.tools import ToolContext

from .tools import (
    fetch_pr_diff,
    read_file_contents,
    get_function_signatures,
    search_docs_by_keyword,
    search_docs_by_file_reference,
    read_doc_file,
)


# ---------------------------------------------------------------------------
# Model configuration — override via environment variables
# ---------------------------------------------------------------------------
_DEFAULT_MODEL = "gemini-2.5-flash"

ANALYZER_MODEL = os.environ.get("PR_DOCS_ANALYZER_MODEL", _DEFAULT_MODEL)
MAPPER_MODEL = os.environ.get("PR_DOCS_MAPPER_MODEL", _DEFAULT_MODEL)
FINDER_MODEL = os.environ.get("PR_DOCS_FINDER_MODEL", _DEFAULT_MODEL)
WRITER_MODEL = os.environ.get("PR_DOCS_WRITER_MODEL", _DEFAULT_MODEL)
REVIEWER_MODEL = os.environ.get("PR_DOCS_REVIEWER_MODEL", _DEFAULT_MODEL)


# ---------------------------------------------------------------------------
# Escalation tool — used by quality_reviewer to exit the refinement loop
# ---------------------------------------------------------------------------

def approve_suggestions(tool_context: ToolContext) -> dict:
    """
    Approve the documentation suggestions and exit the review loop.

    Call this tool ONLY when all suggestions pass all quality criteria
    (accuracy, completeness, tone, specificity, conciseness). Do NOT call
    this if any suggestion has issues — provide feedback instead.

    Returns:
        dict confirming the approval.
    """
    tool_context.actions.escalate = True
    return {"approved": True, "message": "Suggestions approved. Exiting review loop."}


# ---------------------------------------------------------------------------
# Step 1: PR Analysis
# ---------------------------------------------------------------------------

PR_ANALYZER_INSTRUCTION = """\
You analyze GitHub Pull Requests to extract structured change summaries.

Given a PR URL from the user:

1. Use the `fetch_pr_diff` tool to get the PR metadata and structured change
   summary.  The tool parses the diff internally and returns per-file data
   including change types, functions touched, and hunk line-ranges — no raw
   diff text is returned.
2. Synthesize the structured results into a clear JSON summary.

Focus on changes that affect:
- Public API surface (new/changed/removed functions, parameters, return types)
- User-facing behavior (different outputs, new error cases, performance changes)
- Configuration options (new settings, changed defaults)
- Documented features (anything a user reading the docs would care about)

Filter out:
- Internal refactoring with no external impact
- Test-only changes (files in tests/, test_*, *_test.py)
- CI/CD configuration changes (.github/, .circleci/, etc.)
- Dependency updates (requirements.txt, package.json) unless they change public API

Output a JSON object with this structure:
{
  "pr_number": <int>,
  "pr_title": "<string>",
  "overall_summary": "<one-sentence summary of what this PR does>",
  "files": [
    {
      "path": "<file path>",
      "change_type": "added|modified|deleted|renamed",
      "functions_touched": ["<function names>"],
      "summary": "<what changed in this file and why it matters>"
    }
  ]
}
"""

pr_analyzer = Agent(
    name="pr_analyzer",
    model=ANALYZER_MODEL,
    instruction=PR_ANALYZER_INSTRUCTION,
    tools=[fetch_pr_diff],
    output_key="pr_summary",
)


# ---------------------------------------------------------------------------
# Step 2a: Code Mapper (runs in parallel)
# ---------------------------------------------------------------------------

CODE_MAPPER_INSTRUCTION = """\
You are a code analyst. Your job is to understand the SEMANTIC meaning of
code changes — not just which lines moved, but what the changes mean for
users and documentation.

The PR analysis (including changed files) is: {pr_summary}

For each file in the PR analysis's files list:

1. Use `read_file_contents` to read the current version of the file.
2. Use `get_function_signatures` to extract all function/class signatures.
3. For each function or class that was touched, determine:
   - What changed semantically (new parameter, changed default, removed method, etc.)
   - Whether this is user-facing (affects public API, CLI, config) or internal-only
   - The severity: "breaking_change", "new_feature", "behavioral_change", or "cosmetic"

Classify each change's doc relevance:
- "high": Breaking changes, new public API, changed defaults
- "medium": New features, behavioral changes
- "low": Cosmetic changes, internal refactoring
- "none": No documentation impact

CRITICAL — Exception analysis:
- Distinguish between exceptions RAISED TO THE CALLER vs exceptions caught
  internally. Only list exceptions the caller will actually see.
- Example: if a function catches urllib.error.URLError and re-raises as
  RetryError, the caller sees RetryError — NOT URLError or ConnectionError.
- If a code change removes a previously-raised exception (e.g., ConnectionError
  is now caught and converted to RetryError), note this explicitly as a
  behavioral change.

CRITICAL — Import paths:
- Determine the ACTUAL Python import path for each public class/exception
  by examining the module structure (file path + __init__.py exports).
- Example: if RetryError is defined in src/example_lib/client.py, the import
  path is "example_lib.client.RetryError" — NOT "example_lib.exceptions.RetryError"
  unless it is re-exported from an exceptions module.

CRITICAL — Parameter semantics:
- Do not just repeat parameter names. Describe the PRECISE semantics.
- For counts, clarify whether the value includes the initial attempt.
  Example: if max_retries=3 means 3 total attempts (including the first),
  state "3 total attempts (1 initial + 2 retries)". Read the loop logic
  (e.g., `range(max_retries)`) to determine the exact meaning.
- For multipliers/factors, give the formula and an example calculation.

Output a JSON list:
[
  {
    "file": "<path>",
    "function": "<function or class name>",
    "change_type": "<semantic change type>",
    "description": "<what changed and what it means>",
    "user_facing_impact": "high|medium|low|none",
    "doc_relevance": "<why this does or doesn't need doc updates>",
    "import_path": "<actual Python import path, e.g. 'example_lib.client.RetryError'>",
    "exceptions_raised": ["<exceptions the caller will see — NOT internally caught ones>"],
    "parameter_semantics": {
      "<param_name>": "<precise meaning, e.g. 'total attempt count including initial request'>"
    }
  }
]

The last three fields (import_path, exceptions_raised, parameter_semantics)
may be omitted for entries with user_facing_impact "none".

Mark internal refactoring explicitly as user_facing_impact "none" so
downstream agents can skip it.
"""

code_mapper = Agent(
    name="code_mapper",
    model=MAPPER_MODEL,
    instruction=CODE_MAPPER_INSTRUCTION,
    tools=[read_file_contents, get_function_signatures],
    output_key="code_analysis",
)


# ---------------------------------------------------------------------------
# Step 2b: Doc Finder (runs in parallel)
# ---------------------------------------------------------------------------

DOC_FINDER_INSTRUCTION = """\
You find documentation that needs updating based on code changes.

The PR summary (including changed files) is: {pr_summary}

Follow this process:

1. Extract search terms from the changes: function names, class names,
   module names, configuration keys, and key concepts.
2. Use `search_docs_by_keyword` to find docs mentioning these terms.
3. Use `search_docs_by_file_reference` to find docs that reference the
   changed source files (by path, module name, or class name).
4. For each match found, use `read_doc_file` to read the actual doc content
   and verify the reference is substantive — not just a passing mention in
   a changelog, auto-generated index, or unrelated example.
5. Filter out tangential mentions. Only keep doc sections that would
   genuinely need updating given the nature of the code changes.
6. Rank remaining matches by update urgency:
   - "high": Docs describe behavior that changed or was removed
   - "medium": Docs are incomplete (missing new features/parameters)
   - "low": Docs could be improved but aren't wrong

Output a JSON list:
[
  {
    "doc_path": "<path to doc file>",
    "section_heading": "<heading of the relevant section>",
    "line_range": [<start>, <end>],
    "current_content_snippet": "<relevant excerpt from the doc>",
    "reason_for_update": "<why this section needs updating>",
    "urgency": "high|medium|low"
  }
]

If no documentation needs updating, return an empty list [] with a brief
explanation of why.
"""

doc_finder = Agent(
    name="doc_finder",
    model=FINDER_MODEL,
    instruction=DOC_FINDER_INSTRUCTION,
    tools=[search_docs_by_keyword, search_docs_by_file_reference, read_doc_file],
    output_key="relevant_doc_sections",
)


# ---------------------------------------------------------------------------
# Step 2: Parallel Research Phase
# ---------------------------------------------------------------------------

research_phase = ParallelAgent(
    name="research_phase",
    sub_agents=[code_mapper, doc_finder],
)


# ---------------------------------------------------------------------------
# Step 3a: Doc Writer (runs in LoopAgent)
# ---------------------------------------------------------------------------

DOC_WRITER_INSTRUCTION = """\
You write concrete documentation updates by producing the COMPLETE updated
file content for each doc file that needs changes.

Inputs:
- Code change analysis: {code_analysis}
- Doc sections needing updates: {relevant_doc_sections}
- Previous reviewer feedback (if revision): {reviewer_feedback?}

Process:

1. Group the relevant doc sections by file path. You will produce ONE output
   entry per doc file, not per section.
2. For each doc file:
   a. Use `read_doc_file` to read the ENTIRE current file content.
   b. Understand the existing tone, structure, heading hierarchy, and
      terminology.
   c. Apply ALL needed changes for this file — updates, additions, removals —
      into a single complete rewrite of the file.
   d. Place new sections in the LOGICAL location within the document (e.g.,
      a retry configuration section should go near other configuration docs,
      not appended at the end).
   e. Provide a changes_summary listing what was changed and why.

CRITICAL — Preserve unchanged content:
- Copy all text that does NOT need updating exactly as-is, character for
  character. Do NOT rephrase, reformat, or "improve" content that is not
  related to the code changes.
- The only differences between the original file and your suggested_content
  should be the specific documentation updates required by the code changes.

CRITICAL — Markdown formatting rules:
- Always include a blank line BEFORE and AFTER headings (## , ### , etc.).
- Always include a blank line after a list block before starting a new section.
- Ensure every file ends with exactly one trailing newline character.
- Use consistent indentation (spaces, not tabs) in code blocks.

CRITICAL — Import paths and code examples:
- When writing import statements in code examples, use the import_path field
  from the code_analysis — NEVER copy import paths from existing docs without
  verifying them against code_analysis.
- Example: if code_analysis says import_path is "example_lib.client.RetryError",
  write `from example_lib.client import RetryError`, NOT
  `from example_lib.exceptions import RetryError` even if existing docs say that.

CRITICAL — Exception documentation:
- ONLY document exceptions listed in the exceptions_raised field from
  code_analysis. Do NOT document exceptions that are caught internally
  and never seen by the caller.
- If existing docs mention an exception that is NOT in exceptions_raised,
  that is a documentation error — remove or correct it.

CRITICAL — Parameter precision:
- Use the parameter_semantics field from code_analysis to describe parameters
  accurately. Do not guess or infer semantics from parameter names alone.
- For retry counts, clearly state whether the value means "total attempts"
  or "retries after the initial attempt". Get this from code_analysis.

CRITICAL — Spelling and grammar:
- Use standard English spelling: "flaky" not "flakey", "color" not "colour"
  (unless the codebase consistently uses British spelling).
- Proofread placeholder paths and variable names in code examples.

If reviewer_feedback is present, this is a REVISION. You MUST address every
point in the feedback. Do not ignore any feedback item. If you disagree with
feedback, explain why in the changes_summary.

If the relevant_doc_sections list is empty, report "No documentation updates
needed" with a brief explanation.

Output a JSON list with ONE entry per doc file:
[
  {
    "doc_path": "<path to doc file>",
    "changes_summary": "<bullet list of what changed and why>",
    "original_content": "<the complete original file content as read>",
    "suggested_content": "<the complete updated file content>"
  }
]
"""

doc_writer = Agent(
    name="doc_writer",
    model=WRITER_MODEL,
    instruction=DOC_WRITER_INSTRUCTION,
    tools=[read_doc_file],
    output_key="doc_suggestions",
)


# ---------------------------------------------------------------------------
# Step 3b: Quality Reviewer (runs in LoopAgent, after doc_writer)
# ---------------------------------------------------------------------------

QUALITY_REVIEWER_INSTRUCTION = """\
You are a documentation quality reviewer. Your job is to evaluate doc update
suggestions for accuracy, completeness, and quality.

Each suggestion in {doc_suggestions} now contains the COMPLETE original and
suggested file content. Evaluate them against the code changes in
{code_analysis} and the original doc sections in {relevant_doc_sections}.

Apply these criteria strictly:

1. ACCURACY: Do the changes correctly reflect the code changes? Cross-check:
   - Parameter names and types match the code analysis
   - Default values are correct
   - Behavioral descriptions match what the code actually does
   - No hallucinated details that aren't in the code analysis
   This is the MOST IMPORTANT criterion. Inaccurate docs are worse than
   missing docs.

   IMPORT PATH VALIDATION: If the suggested content contains import statements
   or references to module paths (e.g., `from foo.bar import Baz`), verify
   them against the import_path field in code_analysis. REJECT any suggestion
   that uses an import path not confirmed by code_analysis. This is a common
   source of errors — LLMs often invent plausible-sounding but wrong import
   paths (e.g., "example_lib.exceptions" when the real path is
   "example_lib.client").

   EXCEPTION VALIDATION: If the suggested content documents exceptions, verify
   them against the exceptions_raised field in code_analysis. REJECT any
   suggestion that documents an exception the code does not raise to the
   caller. Pay special attention to exceptions that are caught internally and
   converted to different exception types (e.g., ConnectionError caught and
   re-raised as RetryError).

   PARAMETER SEMANTICS: If the suggested content describes a parameter
   (especially counts, limits, or factors), verify the description matches
   the parameter_semantics field in code_analysis. REJECT descriptions with
   off-by-one errors (e.g., saying "retry attempts" when the parameter
   means "total attempts including the initial request").

2. NO UNINTENDED CHANGES: Compare original_content to suggested_content.
   The ONLY differences should be the specific documentation updates required
   by the code changes. REJECT if the writer has:
   - Rephrased or reformatted text that was not related to the code changes
   - Removed content that should still be present
   - Changed heading levels, list formatting, or structure unnecessarily
   - Added content not related to the code changes
   This criterion catches LLM tendency to "improve" unrelated text.

3. COMPLETENESS: Are all relevant doc sections addressed? Check if there are
   code changes with user_facing_impact "high" or "medium" in the code
   analysis that have no corresponding update in any suggested_content.

4. TONE & STYLE: Does the updated text match the existing documentation's
   voice, formatting conventions, and terminology?

5. SPECIFICITY: Are changes concrete? The suggested_content must be a
   complete, ready-to-commit file — not placeholders or TODOs.

6. CONCISENESS: Are the added/changed sections appropriately scoped? They
   should cover what changed without over-explaining or adding unnecessary
   padding.

7. MARKDOWN FORMATTING: Check that suggested_content follows proper Markdown:
   - Blank line before and after every heading (##, ###, etc.)
   - Blank line after a list block before a new section
   - Consistent indentation in code blocks
   - No trailing whitespace on lines
   - File ends with a single trailing newline

8. SPELLING & GRAMMAR: Flag obvious typos or non-standard spellings
   (e.g., "flakey" should be "flaky").

9. CHANGES_SUMMARY: Every suggestion MUST have a non-empty changes_summary
   that explains what was changed and why. REJECT any suggestion with an
   empty or missing changes_summary.

10. SECTION PLACEMENT: For new sections, verify they are placed in a logical
    location within the document — not just appended at the end. A retry
    configuration section should be near other configuration docs, error
    handling examples near other usage examples, etc.

Decision:
- If ALL suggestions pass ALL criteria: call the `approve_suggestions` tool
  to approve and exit the review loop.
- If ANY suggestion has issues: output specific, actionable feedback. Do NOT
  rewrite the suggestions yourself. Be precise about what is wrong and what
  the writer should fix.

When providing feedback, output a JSON object:
{
  "approved": false,
  "issues": [
    {
      "suggestion_index": <int>,
      "criterion": "<which criterion failed>",
      "issue": "<what is wrong>",
      "fix_instruction": "<exactly what the writer should change>"
    }
  ],
  "overall_feedback": "<summary>"
}
"""

quality_reviewer = Agent(
    name="quality_reviewer",
    model=REVIEWER_MODEL,
    instruction=QUALITY_REVIEWER_INSTRUCTION,
    tools=[approve_suggestions],
    output_key="reviewer_feedback",
)


# ---------------------------------------------------------------------------
# Step 3: Refinement Loop
# ---------------------------------------------------------------------------

refinement_loop = LoopAgent(
    name="refinement_loop",
    max_iterations=3,
    sub_agents=[doc_writer, quality_reviewer],
)


# ---------------------------------------------------------------------------
# Top-Level Pipeline
# ---------------------------------------------------------------------------

# NOTE: auto_apply (creating a doc PR from suggestions) is handled
# deterministically in run_pipeline.py after the pipeline completes,
# rather than by an LLM agent.  This avoids the unreliability of relying
# on an LLM to conditionally call a tool.

root_agent = SequentialAgent(
    name="pr_docs_pipeline",
    sub_agents=[pr_analyzer, research_phase, refinement_loop],
    description=(
        "Analyzes a GitHub PR, finds relevant documentation, and suggests "
        "concrete documentation updates with iterative quality review."
    ),
)
