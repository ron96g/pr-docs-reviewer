"""Driver script for the Full Documentation Generator pipeline.

Usage:
    # Minimal — scan a local repo and generate docs:
    REPO_PATH=/path/to/repo python run_docs_generator.py

    # With a GitHub repo:
    REPO=owner/repo python run_docs_generator.py

    # With a documentation spec:
    REPO_PATH=/path/to/repo DOC_SPEC=doc_spec.yaml python run_docs_generator.py

    # With auto-apply (creates a branch + PR with generated docs):
    REPO=owner/repo AUTO_APPLY=true python run_docs_generator.py

Architecture:
    Phase 1: Run scan_plan_pipeline (codebase_scanner -> doc_planner)
             to populate state["codebase_map"] and state["doc_plan"].

    Phase 2: For each page in doc_plan["pages"] (in suggested_order),
             run page_refinement_loop to generate and review the page.
             After each page, collect the result into state["completed_pages"].

    Phase 3 (optional): If AUTO_APPLY is true, call apply_suggestions()
             to commit generated docs to a branch and open a PR.
"""

import asyncio
import json
import logging
import os
import re
import sys

import yaml

from google.adk.runners import InMemoryRunner
from google.genai import types

from docs_generator.agent import scan_plan_pipeline, page_refinement_loop
from shared.tools.backend import set_backend
from shared.tools.apply_doc_updates import apply_suggestions

logging.basicConfig(level=logging.INFO, format="%(name)s: %(message)s")
logger = logging.getLogger(__name__)


def _strip_markdown_fences(text: str) -> str:
    """Strip markdown code fences (```json ... ```) from LLM output."""
    stripped = re.sub(r"^```(?:json|yaml)?\s*\n?", "", text.strip())
    stripped = re.sub(r"\n?```\s*$", "", stripped)
    return stripped.strip()


def _load_doc_spec(path: str) -> dict | None:
    """Load a YAML documentation spec file, or return None."""
    if not path:
        return None
    try:
        with open(path) as f:
            spec = yaml.safe_load(f)
        logger.info("Loaded doc spec from %s (%d pages defined)",
                     path, len(spec.get("pages", [])))
        return spec
    except Exception as e:
        logger.warning("Could not load doc spec from %s: %s", path, e)
        return None


def _parse_json_state(raw: str | list | dict) -> list | dict:
    """Parse a state value that might be JSON-in-a-string or already parsed."""
    if isinstance(raw, (list, dict)):
        return raw
    if isinstance(raw, str):
        try:
            return json.loads(_strip_markdown_fences(raw))
        except (json.JSONDecodeError, TypeError):
            return {}
    return {}


async def main():
    # ----- Configuration from environment -----
    repo_path = os.environ.get("REPO_PATH", "")
    repo = os.environ.get("REPO", "")  # owner/repo for GitHub mode
    doc_spec_path = os.environ.get("DOC_SPEC", "")
    auto_apply = os.environ.get("AUTO_APPLY", "false").lower() == "true"
    output_dir = os.environ.get("OUTPUT_DIR", "")  # override doc_plan output_dir

    if not repo_path and not repo:
        print("ERROR: Set REPO_PATH (local) or REPO (owner/repo for GitHub).",
              file=sys.stderr)
        sys.exit(1)

    # ----- Set up backend -----
    if repo_path:
        from shared.tools.local_backend import LocalBackend
        set_backend(LocalBackend(repo_path))
        logger.info("Using LocalBackend at %s", repo_path)
    else:
        from shared.tools.github_api_backend import GitHubAPIBackend
        parts = repo.split("/", 1)
        if len(parts) != 2 or not all(parts):
            print("ERROR: REPO must be in 'owner/repo' format.", file=sys.stderr)
            sys.exit(1)
        owner, repo_name = parts
        backend = GitHubAPIBackend()
        backend.configure(owner=owner, repo=repo_name, pr_number=0)
        set_backend(backend)
        logger.info("Using GitHubAPIBackend for %s", repo)

    # ----- Load optional doc spec -----
    doc_spec = _load_doc_spec(doc_spec_path)

    # ----- Initial session state -----
    initial_state: dict = {
        "repo": repo or repo_path,
    }
    if doc_spec:
        initial_state["doc_spec"] = json.dumps(doc_spec)
    else:
        initial_state["doc_spec"] = ""

    # =====================================================================
    # PHASE 1: Scan & Plan
    # =====================================================================
    app_name = "docs-generator"
    runner = InMemoryRunner(agent=scan_plan_pipeline, app_name=app_name)
    session = await runner.session_service.create_session(
        app_name=app_name,
        user_id="docs-generator",
        state=initial_state,
    )

    logger.info("=== Phase 1: Scanning codebase and planning docs ===")

    async for event in runner.run_async(
        user_id="docs-generator",
        session_id=session.id,
        new_message=types.Content(
            role="user",
            parts=[types.Part(text="Scan this codebase and plan documentation.")],
        ),
    ):
        if event.content and event.content.parts:
            for part in event.content.parts:
                if hasattr(part, "text") and part.text:
                    logger.debug("Phase 1 output: %s", part.text[:200])

    # Reload session state
    session = await runner.session_service.get_session(
        app_name=app_name,
        user_id="docs-generator",
        session_id=session.id,
    )
    state = session.state or {}

    codebase_map = _parse_json_state(state.get("codebase_map", "{}"))
    doc_plan = _parse_json_state(state.get("doc_plan", "{}"))

    # The doc_plan might be nested: {"doc_plan": {...}} or flat.
    if "doc_plan" in doc_plan and isinstance(doc_plan["doc_plan"], dict):
        doc_plan = doc_plan["doc_plan"]

    pages = doc_plan.get("pages", [])
    suggested_order = doc_plan.get("suggested_order", [p.get("path") for p in pages])

    if not pages:
        logger.error("Doc planner returned no pages. State keys: %s",
                      list(state.keys()))
        print("ERROR: Doc planner did not produce any pages.", file=sys.stderr)
        sys.exit(1)

    # Apply output_dir override
    plan_output_dir = output_dir or doc_plan.get("output_dir", "docs/")

    logger.info("Doc plan: %d pages, output_dir=%s", len(pages), plan_output_dir)
    for p in pages:
        logger.info("  - %s (%s): %s", p.get("path"), p.get("action"), p.get("title"))

    # =====================================================================
    # PHASE 2: Per-page generation
    # =====================================================================
    logger.info("=== Phase 2: Generating documentation pages ===")

    # Build a lookup for pages by path for ordering
    pages_by_path = {p["path"]: p for p in pages}
    ordered_pages = []
    for path in suggested_order:
        if path in pages_by_path:
            ordered_pages.append(pages_by_path.pop(path))
    # Append any pages not in suggested_order
    ordered_pages.extend(pages_by_path.values())

    completed_pages: list[dict] = []
    all_suggestions: list[dict] = []  # For auto-apply: [{doc_path, suggested_content, ...}]

    for i, page in enumerate(ordered_pages):
        page_path = page.get("path", f"page_{i}.md")
        logger.info("--- Generating page %d/%d: %s ---", i + 1, len(ordered_pages), page_path)

        # Set up per-page state: inject codebase_map, current_page, completed_pages
        page_state = dict(state)  # Copy existing state (includes codebase_map, doc_plan)
        page_state["current_page"] = json.dumps(page)
        page_state["completed_pages"] = json.dumps(completed_pages)
        page_state["page_reviewer_feedback"] = ""  # Clear for fresh loop
        page_state["current_page_draft"] = ""  # Clear for fresh loop

        page_runner = InMemoryRunner(agent=page_refinement_loop, app_name=app_name)
        page_session = await page_runner.session_service.create_session(
            app_name=app_name,
            user_id="docs-generator",
            state=page_state,
        )

        page_prompt = (
            f"Generate the documentation page: {page.get('title', page_path)}\n"
            f"Output path: {page_path}\n"
            f"Scope: {page.get('scope', 'See assignment.')}"
        )

        async for event in page_runner.run_async(
            user_id="docs-generator",
            session_id=page_session.id,
            new_message=types.Content(
                role="user",
                parts=[types.Part(text=page_prompt)],
            ),
        ):
            pass  # We read results from state, not from streamed text

        # Reload page session state
        page_session = await page_runner.session_service.get_session(
            app_name=app_name,
            user_id="docs-generator",
            session_id=page_session.id,
        )
        page_result_state = page_session.state or {}

        # Extract the generated page draft
        draft_raw = page_result_state.get("current_page_draft", "{}")
        draft = _parse_json_state(draft_raw)

        if isinstance(draft, dict) and draft.get("content"):
            completed_pages.append({
                "path": draft.get("page_path", page_path),
                "title": draft.get("title", page.get("title", "")),
                "summary": draft.get("summary", ""),
            })
            all_suggestions.append({
                "doc_path": draft.get("page_path", page_path),
                "suggested_content": draft["content"],
                "changes_summary": f"Generated: {draft.get('title', page_path)}",
            })
            logger.info("  Page complete: %s (%d chars)",
                         draft.get("page_path", page_path),
                         len(draft["content"]))
        else:
            logger.warning("  Page generation failed or returned empty for %s", page_path)
            logger.debug("  Draft state: %s", str(draft_raw)[:500])

    # =====================================================================
    # Summary
    # =====================================================================
    logger.info("=== Generation complete: %d/%d pages generated ===",
                 len(all_suggestions), len(ordered_pages))

    for s in all_suggestions:
        logger.info("  - %s (%d chars)", s["doc_path"], len(s["suggested_content"]))

    # =====================================================================
    # PHASE 3 (optional): Auto-apply
    # =====================================================================
    if auto_apply and all_suggestions:
        logger.info("=== Phase 3: Auto-applying generated docs ===")
        apply_result = apply_suggestions(
            suggestions=all_suggestions,
            repo=repo or repo_path,
            base_branch=os.environ.get("BASE_BRANCH", "main"),
            branch_prefix="docs/generate",
        )
        logger.info("Apply result: status=%s, files_updated=%s",
                      apply_result.get("status"),
                      apply_result.get("files_updated", []))
        if apply_result.get("doc_pr_url"):
            print(f"\nDocumentation PR created: {apply_result['doc_pr_url']}")
        if apply_result.get("error_message"):
            logger.warning("Apply error: %s", apply_result["error_message"])
    elif not auto_apply and all_suggestions:
        # Write generated docs to local files
        for s in all_suggestions:
            out_path = s["doc_path"]
            os.makedirs(os.path.dirname(out_path) or ".", exist_ok=True)
            with open(out_path, "w") as f:
                f.write(s["suggested_content"])
            logger.info("Wrote %s", out_path)
        print(f"\nGenerated {len(all_suggestions)} documentation files locally.")

    # =====================================================================
    # Output JSON summary (useful for CI integration)
    # =====================================================================
    summary = {
        "status": "success" if all_suggestions else "no_output",
        "pages_planned": len(ordered_pages),
        "pages_generated": len(all_suggestions),
        "files": [s["doc_path"] for s in all_suggestions],
    }
    print(f"\n{json.dumps(summary, indent=2)}")

    # Write to GITHUB_OUTPUT if available (CI mode)
    github_output = os.environ.get("GITHUB_OUTPUT", "")
    if github_output:
        summary_json = json.dumps(summary)
        with open(github_output, "a") as f:
            f.write(f"summary<<__SUMMARY_EOF__\n{summary_json}\n__SUMMARY_EOF__\n")
            f.write(f"status={summary['status']}\n")
            f.write(f"pages_generated={summary['pages_generated']}\n")


if __name__ == "__main__":
    asyncio.run(main())
