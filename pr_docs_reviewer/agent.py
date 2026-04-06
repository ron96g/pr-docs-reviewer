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
    apply_doc_updates,
)


# ---------------------------------------------------------------------------
# Model configuration — override via environment variables
# ---------------------------------------------------------------------------
_DEFAULT_MODEL = "gemini-3-flash-preview"

ANALYZER_MODEL = os.environ.get("PR_DOCS_ANALYZER_MODEL", _DEFAULT_MODEL)
MAPPER_MODEL = os.environ.get("PR_DOCS_MAPPER_MODEL", _DEFAULT_MODEL)
FINDER_MODEL = os.environ.get("PR_DOCS_FINDER_MODEL", _DEFAULT_MODEL)
WRITER_MODEL = os.environ.get("PR_DOCS_WRITER_MODEL", _DEFAULT_MODEL)
REVIEWER_MODEL = os.environ.get("PR_DOCS_REVIEWER_MODEL", _DEFAULT_MODEL)
APPLIER_MODEL = os.environ.get("PR_DOCS_APPLIER_MODEL", _DEFAULT_MODEL)


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

Output a JSON list:
[
  {
    "file": "<path>",
    "function": "<function or class name>",
    "change_type": "<semantic change type>",
    "description": "<what changed and what it means>",
    "user_facing_impact": "high|medium|low|none",
    "doc_relevance": "<why this does or doesn't need doc updates>"
  }
]

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
You write concrete documentation update suggestions.

Inputs:
- Code change analysis: {code_analysis}
- Doc sections needing updates: {relevant_doc_sections}
- Previous reviewer feedback (if revision): {reviewer_feedback?}

For each relevant doc section:

1. Use `read_doc_file` to get the full context around the section — read the
   entire doc file so you understand the tone, structure, and terminology.
2. Match the existing documentation's writing style precisely.
3. Determine the change type:
   - "update_text": Modify existing description
   - "add_section": Add a new section
   - "add_example": Add a code example
   - "update_code_sample": Fix an existing code sample
   - "add_note": Add a note or tip
   - "add_warning": Add a warning about breaking changes
   - "remove_section": Remove a section for deleted features
4. Write the suggested new or replacement text.
5. Provide a clear rationale linking the suggestion to a specific code change.

If reviewer_feedback is present, this is a REVISION. You MUST address every
point in the feedback. Do not ignore any feedback item. If you disagree with
feedback, explain why in the rationale.

If the relevant_doc_sections list is empty, report "No documentation updates
needed" with a brief explanation.

Output a JSON list:
[
  {
    "doc_path": "<path>",
    "section": "<section heading>",
    "change_type": "<type from above>",
    "current_text": "<the text that exists now (or null for new sections)>",
    "suggested_text": "<your suggested replacement or new text>",
    "rationale": "<why this change is needed, linked to specific code change>"
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

Evaluate the suggestions in {doc_suggestions} against the code changes in
{code_analysis} and the original doc sections in {relevant_doc_sections}.

Apply these criteria strictly:

1. ACCURACY: Do suggestions correctly reflect the code changes? Cross-check:
   - Parameter names and types match the code analysis
   - Default values are correct
   - Behavioral descriptions match what the code actually does
   - No hallucinated details that aren't in the code analysis
   This is the MOST IMPORTANT criterion. Inaccurate docs are worse than
   missing docs.

2. COMPLETENESS: Are all relevant doc sections addressed? Check if there are
   code changes with user_facing_impact "high" or "medium" in the code
   analysis that have no corresponding doc suggestion.

3. TONE & STYLE: Does the suggested text match the existing documentation's
   voice, formatting conventions, and terminology? Check the
   current_content_snippet for reference.

4. SPECIFICITY: Are suggestions concrete and actionable? Vague suggestions
   like "update the docs to reflect changes" are NOT acceptable. Every
   suggestion must include actual replacement text.

5. CONCISENESS: Is the suggested text appropriately scoped? It should cover
   what changed without over-explaining or adding unnecessary padding.

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
# Step 4: Doc Applier (opt-in — only runs when auto_apply is set)
# ---------------------------------------------------------------------------

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
"""

doc_applier = Agent(
    name="doc_applier",
    model=APPLIER_MODEL,
    instruction=DOC_APPLIER_INSTRUCTION,
    tools=[apply_doc_updates],
    output_key="apply_result",
)


# ---------------------------------------------------------------------------
# Top-Level Pipeline
# ---------------------------------------------------------------------------

root_agent = SequentialAgent(
    name="pr_docs_pipeline",
    sub_agents=[pr_analyzer, research_phase, refinement_loop, doc_applier],
    description=(
        "Analyzes a GitHub PR, finds relevant documentation, and suggests "
        "concrete documentation updates with iterative quality review. "
        "Optionally applies changes as a PR when auto_apply is enabled."
    ),
)
