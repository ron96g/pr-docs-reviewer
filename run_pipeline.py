"""Driver script for the PR Documentation Reviewer pipeline.

Reads configuration from environment variables and runs the ADK pipeline,
writing results to GitHub Actions outputs.
"""

import asyncio
import json
import os
import sys

from google.adk.runners import InMemoryRunner

from pr_docs_reviewer.agent import root_agent


async def main():
    pr_url = os.environ["PR_URL"]
    auto_apply = os.environ.get("AUTO_APPLY", "false").lower() == "true"

    runner = InMemoryRunner(agent=root_agent, app_name="pr-docs-reviewer")
    session = await runner.session_service.create_session(
        app_name="pr-docs-reviewer",
        user_id="github-action",
        state={"auto_apply": auto_apply},
    )

    final_text = ""
    async for event in runner.run_async(
        user_id="github-action",
        session_id=session.id,
        new_message=f"Review this PR: {pr_url}",
    ):
        if event.content and event.content.parts:
            for part in event.content.parts:
                if hasattr(part, "text") and part.text:
                    final_text = part.text

    # Reload session to read final state
    session = await runner.session_service.get_session(
        app_name="pr-docs-reviewer",
        user_id="github-action",
        session_id=session.id,
    )

    state = session.state or {}
    suggestions = state.get("doc_suggestions", "[]")
    doc_pr_url = ""
    apply_result = state.get("apply_result", "")

    if isinstance(apply_result, dict):
        doc_pr_url = apply_result.get("doc_pr_url", "")
    elif isinstance(apply_result, str):
        try:
            parsed = json.loads(apply_result)
            doc_pr_url = parsed.get("doc_pr_url", "")
        except (json.JSONDecodeError, TypeError):
            pass

    # Determine status
    if isinstance(suggestions, str):
        try:
            parsed_suggestions = json.loads(suggestions)
        except (json.JSONDecodeError, TypeError):
            parsed_suggestions = []
    else:
        parsed_suggestions = suggestions

    if not parsed_suggestions:
        status = "no_changes"
    else:
        status = "success"

    # Write outputs
    with open(os.environ["GITHUB_OUTPUT"], "a") as f:
        if isinstance(suggestions, list):
            suggestions = json.dumps(suggestions)
        f.write(f"suggestions={suggestions}\n")
        f.write(f"doc_pr_url={doc_pr_url}\n")
        f.write(f"status={status}\n")

    # Print summary to the action log
    print(f"\n--- PR Documentation Review Complete ---")
    print(f"Status: {status}")
    print(f"Suggestions: {len(parsed_suggestions)}")
    if doc_pr_url:
        print(f"Doc PR: {doc_pr_url}")

    if final_text:
        summary_file = os.environ.get("GITHUB_STEP_SUMMARY", "")
        if summary_file:
            with open(summary_file, "a") as f:
                f.write("## PR Documentation Review\n\n")
                f.write(final_text)
                f.write("\n")


if __name__ == "__main__":
    asyncio.run(main())
