"""
Full Documentation Generator — Multi-Agent Pipeline

Architecture (runner-driven per-page loop):

    Phase 1 — Scan & Plan (ADK orchestration):
        SequentialAgent("docs_scan_plan")
        +-- codebase_scanner (LlmAgent)
        +-- doc_planner (LlmAgent)

    Phase 2 — Per-page write+review (Python driver loop):
        For each page in state["doc_plan"]["pages"]:
            LoopAgent("page_refinement", max_iterations=3)
            +-- doc_generator (LlmAgent)
            +-- doc_quality_reviewer (LlmAgent)

All inter-agent communication flows through session.state via output_key.
"""

import os

from google.adk.agents import Agent, SequentialAgent, LoopAgent
from google.adk.tools import ToolContext

from shared.tools import (
    list_source_files,
    read_file_contents,
    get_function_signatures,
    search_docs_by_keyword,
    read_doc_file,
)


# ---------------------------------------------------------------------------
# Model configuration — override via environment variables
# ---------------------------------------------------------------------------
_DEFAULT_MODEL = "gemini-2.5-flash"

SCANNER_MODEL = os.environ.get("DOCS_GEN_SCANNER_MODEL", _DEFAULT_MODEL)
PLANNER_MODEL = os.environ.get("DOCS_GEN_PLANNER_MODEL", _DEFAULT_MODEL)
GENERATOR_MODEL = os.environ.get("DOCS_GEN_GENERATOR_MODEL", _DEFAULT_MODEL)
REVIEWER_MODEL = os.environ.get("DOCS_GEN_REVIEWER_MODEL", _DEFAULT_MODEL)


# ---------------------------------------------------------------------------
# Escalation tool — used by doc_quality_reviewer to exit the refinement loop
# ---------------------------------------------------------------------------

def approve_page(tool_context: ToolContext) -> dict:
    """
    Approve the generated documentation page and exit the review loop.

    Call this tool ONLY when the page passes all quality criteria
    (accuracy, completeness, structure, tone, code examples). Do NOT call
    this if the page has issues — provide feedback instead.

    Returns:
        dict confirming the approval.
    """
    tool_context.actions.escalate = True
    return {"approved": True, "message": "Page approved. Exiting review loop."}


# ---------------------------------------------------------------------------
# Phase 1a: Codebase Scanner
# ---------------------------------------------------------------------------

CODEBASE_SCANNER_INSTRUCTION = """\
You are a codebase analyst. Your job is to discover and map the structure of
a software repository so that documentation can be generated from scratch.

If a documentation spec was provided, it will appear below. Focus your
analysis on modules and APIs relevant to those topics, while still scanning
the full repository for context:
{doc_spec?}

Process:

1. Use `list_source_files` with NO filters first to get a broad inventory of
   the repository.

2. Identify the project's key directories and modules. Group files by:
   - Package / module boundaries
   - Source vs test vs config vs docs
   - Public API surface vs internal implementation

3. For each module that appears to contain public API:
   a. Use `read_file_contents` to understand the module's purpose.
   b. Use `get_function_signatures` to extract public functions, classes,
      and their signatures.

4. Determine the project type and ecosystem:
   - Language(s) and framework(s)
   - Entry points (main, CLI, web endpoints)
   - Configuration files and their role

CRITICAL — Focus on the PUBLIC surface:
- Skip test files, CI configs, and build scripts unless they are user-facing.
- For each public function/class, note: name, signature, module, and a brief
  purpose (one sentence from reading the code).
- Identify key types/protocols/interfaces that users interact with.

Output a JSON object:
{{
  "project_name": "<detected project name>",
  "language": "<primary language>",
  "frameworks": ["<framework1>", ...],
  "entry_points": ["<entry point paths>"],
  "modules": [
    {{
      "path": "<module path>",
      "purpose": "<one-sentence purpose>",
      "public_api": [
        {{
          "name": "<function/class name>",
          "kind": "function|class|protocol|constant",
          "signature": "<full signature>",
          "purpose": "<one-sentence purpose>"
        }}
      ]
    }}
  ],
  "config_files": ["<paths to config files>"],
  "existing_docs": ["<paths to existing doc files>"]
}}
"""

codebase_scanner = Agent(
    name="codebase_scanner",
    model=SCANNER_MODEL,
    instruction=CODEBASE_SCANNER_INSTRUCTION,
    tools=[list_source_files, read_file_contents, get_function_signatures],
    output_key="codebase_map",
)


# ---------------------------------------------------------------------------
# Phase 1b: Documentation Planner
# ---------------------------------------------------------------------------

DOC_PLANNER_INSTRUCTION = """\
You are a documentation architect. Given a codebase map, you plan the
complete documentation structure — what pages to create, their scope, and
the order to write them.

Inputs:
- Codebase map: {codebase_map}
- Documentation spec (if provided): {doc_spec}

If a doc_spec is provided, use it as the primary guide for page structure,
titles, and scope. Fill in details from the codebase_map. If no doc_spec is
provided, design the documentation structure yourself based on best practices.

Process:

1. Review the codebase_map to understand the project's scope and structure.

2. Use `search_docs_by_keyword` with broad terms (project name, key module
   names) to discover any existing documentation that should be updated
   rather than rewritten from scratch.

3. Use `read_doc_file` to read any existing docs found, to understand what
   already exists and what's missing.

4. Design the documentation structure. For a typical project, consider:
   - **Getting Started / Quickstart**: Installation, basic usage, first example
   - **API Reference**: One page per major module or logical grouping
   - **Configuration**: How to configure the project
   - **Architecture / Concepts**: High-level design, key abstractions
   - **Examples / Tutorials**: Common use cases, recipes
   - **Contributing**: How to develop, test, release (if open source)

5. For each planned page, specify:
   - Output file path (e.g., "docs/getting-started.md")
   - Title and scope (what this page covers)
   - Relevant modules from codebase_map (which modules the generator should
     reference)
   - Dependencies on other pages (e.g., API ref should come after quickstart)
   - Whether this is a NEW page or an UPDATE to an existing doc

CRITICAL — Be practical:
- Don't create a page per function. Group related APIs into logical pages.
- For small projects (< 10 modules), 3-6 pages is usually sufficient.
- For larger projects, aim for 8-15 pages organized into sections.
- Each page should be self-contained enough to be useful on its own.

Output a JSON object:
{{
  "doc_plan": {{
    "output_dir": "<root directory for generated docs, e.g. 'docs/'>",
    "pages": [
      {{
        "path": "<output file path, e.g. 'docs/getting-started.md'>",
        "title": "<page title>",
        "scope": "<one-paragraph description of what this page covers>",
        "relevant_modules": ["<module paths from codebase_map>"],
        "depends_on": ["<paths of pages that should be written first>"],
        "action": "create|update",
        "existing_content_path": "<path to existing doc if action is update, else null>"
      }}
    ],
    "suggested_order": ["<page paths in recommended writing order>"]
  }}
}}
"""

doc_planner = Agent(
    name="doc_planner",
    model=PLANNER_MODEL,
    instruction=DOC_PLANNER_INSTRUCTION,
    tools=[search_docs_by_keyword, read_doc_file],
    output_key="doc_plan",
)


# ---------------------------------------------------------------------------
# Phase 1: Scan & Plan (Sequential)
# ---------------------------------------------------------------------------

scan_plan_pipeline = SequentialAgent(
    name="docs_scan_plan",
    sub_agents=[codebase_scanner, doc_planner],
    description="Scans codebase and plans documentation structure.",
)


# ---------------------------------------------------------------------------
# Phase 2a: Documentation Generator (runs in LoopAgent, per page)
# ---------------------------------------------------------------------------

DOC_GENERATOR_INSTRUCTION = """\
You write complete documentation pages from scratch based on a codebase map
and a specific page assignment.

Inputs:
- Codebase map: {codebase_map}
- Current page assignment: {current_page}
- Previously written pages (for cross-referencing): {completed_pages}
- Reviewer feedback (if revision): {page_reviewer_feedback?}

Process:

1. Read the page assignment to understand the scope, title, and relevant
   modules.

2. For each relevant module listed in the assignment:
   a. Use `read_file_contents` to read the actual source code.
   b. Use `get_function_signatures` to get precise signatures.
   c. Cross-reference with the codebase_map for context.

3. If the assignment's action is "update" and existing_content_path is set,
   use `read_doc_file` to read the existing content first. Preserve what's
   still accurate, update what's changed, and add what's missing.

4. Write the complete page content in Markdown.

CRITICAL — Content quality:
- Write for the USER, not the developer. Explain what things do and why,
  not just how they're implemented.
- Include practical code examples for every major API. Examples should be
  complete and runnable, not fragments.
- Use the project's actual import paths from the codebase_map.
- For functions with multiple parameters, include a parameter table or
  detailed parameter list.
- For classes, document both the constructor and key methods.

CRITICAL — Cross-references:
- When referencing concepts covered in other pages (from completed_pages or
  the doc plan), use relative Markdown links.
- Don't duplicate content from other pages — link to it instead.

CRITICAL — Markdown formatting:
- Always include a blank line BEFORE and AFTER headings.
- Always include a blank line after a list block before a new section.
- Use consistent heading hierarchy (# for title, ## for sections, ### for
  subsections).
- Ensure the file ends with exactly one trailing newline.
- Use fenced code blocks with language identifiers (```python, ```bash, etc.).

If page_reviewer_feedback is present, this is a REVISION. Address every point
in the feedback. Do not ignore any feedback item.

Output a JSON object:
{{
  "page_path": "<output file path from the assignment>",
  "title": "<page title>",
  "content": "<the complete Markdown content of the page>",
  "summary": "<brief summary of what this page covers>"
}}
"""

doc_generator = Agent(
    name="doc_generator",
    model=GENERATOR_MODEL,
    instruction=DOC_GENERATOR_INSTRUCTION,
    tools=[read_file_contents, get_function_signatures, read_doc_file],
    output_key="current_page_draft",
)


# ---------------------------------------------------------------------------
# Phase 2b: Documentation Quality Reviewer (runs in LoopAgent, per page)
# ---------------------------------------------------------------------------

DOC_QUALITY_REVIEWER_INSTRUCTION = """\
You are a documentation quality reviewer. Your job is to evaluate a generated
documentation page for accuracy, completeness, and quality.

Inputs:
- Codebase map: {codebase_map}
- Page assignment: {current_page}
- Page draft: {current_page_draft}

Evaluate the draft against these criteria:

1. ACCURACY: Do code examples, function signatures, parameter descriptions,
   and import paths match the codebase_map? Cross-check:
   - Function names and signatures match exactly
   - Parameter types and defaults are correct
   - Import paths match the actual module structure
   - No hallucinated APIs or features

2. COMPLETENESS: Does the page cover everything listed in the assignment's
   scope? Check:
   - All relevant modules from the assignment are covered
   - Public API items from codebase_map for those modules are documented
   - Key use cases have examples

3. STRUCTURE: Is the page well-organized?
   - Logical heading hierarchy
   - Progressive disclosure (overview -> details -> examples)
   - Related items grouped together

4. CODE EXAMPLES: Are they practical and correct?
   - Complete and runnable (not fragments)
   - Use correct import paths
   - Demonstrate real use cases, not trivial examples
   - Include expected output where helpful

5. TONE & CLARITY: Is the writing clear, concise, and user-focused?
   - Explains "what" and "why", not just "how"
   - Avoids unnecessary jargon
   - Consistent terminology throughout

6. MARKDOWN FORMATTING:
   - Blank lines before and after headings
   - Proper fenced code blocks with language tags
   - Consistent list formatting
   - Single trailing newline at end of file

7. CROSS-REFERENCES: Are links to other pages correct and helpful?
   - Relative links use correct paths
   - No broken references to pages that don't exist yet

Decision:
- If the page passes ALL criteria: call the `approve_page` tool to approve
  and exit the review loop.
- If the page has issues: output specific, actionable feedback. Do NOT
  rewrite the page yourself. Be precise about what is wrong and what the
  generator should fix.

When providing feedback, output a JSON object:
{{
  "approved": false,
  "issues": [
    {{
      "criterion": "<which criterion failed>",
      "issue": "<what is wrong>",
      "fix_instruction": "<exactly what the generator should change>"
    }}
  ],
  "overall_feedback": "<summary>"
}}
"""

doc_quality_reviewer = Agent(
    name="doc_quality_reviewer",
    model=REVIEWER_MODEL,
    instruction=DOC_QUALITY_REVIEWER_INSTRUCTION,
    tools=[approve_page],
    output_key="page_reviewer_feedback",
)


# ---------------------------------------------------------------------------
# Phase 2: Per-page Refinement Loop (instantiated per page by the driver)
# ---------------------------------------------------------------------------

page_refinement_loop = LoopAgent(
    name="page_refinement",
    max_iterations=3,
    sub_agents=[doc_generator, doc_quality_reviewer],
)


# ---------------------------------------------------------------------------
# Exported agents — the driver script uses these directly
# ---------------------------------------------------------------------------

# Phase 1: scan_plan_pipeline is run once to populate codebase_map & doc_plan.
# Phase 2: page_refinement_loop is run once per page by the driver.
#
# We do NOT wrap these in a single root_agent because the per-page iteration
# is handled by the Python driver, not by ADK orchestration.

__all__ = [
    "scan_plan_pipeline",
    "page_refinement_loop",
    "codebase_scanner",
    "doc_planner",
    "doc_generator",
    "doc_quality_reviewer",
]
