#!/usr/bin/env bash
set -euo pipefail

# -----------------------------------------------------------------------
# Entrypoint for the PR Documentation Reviewer GitHub Action.
#
# Reads inputs from environment variables (set by action.yml), runs the
# ADK pipeline, and writes outputs to $GITHUB_OUTPUT.
# -----------------------------------------------------------------------

# Map action inputs to the environment variables the pipeline expects
export GOOGLE_API_KEY="${INPUT_GOOGLE_API_KEY}"
export SOURCE_MODE="local"

# Override model for all agents if provided
if [[ -n "${INPUT_MODEL:-}" ]]; then
    export PR_DOCS_ANALYZER_MODEL="${INPUT_MODEL}"
    export PR_DOCS_MAPPER_MODEL="${INPUT_MODEL}"
    export PR_DOCS_FINDER_MODEL="${INPUT_MODEL}"
    export PR_DOCS_WRITER_MODEL="${INPUT_MODEL}"
    export PR_DOCS_REVIEWER_MODEL="${INPUT_MODEL}"
    export PR_DOCS_APPLIER_MODEL="${INPUT_MODEL}"
fi

# Resolve docs path
export DOCS_PATH="${INPUT_DOCS_PATH:-docs/}"

# Determine auto_apply
AUTO_APPLY="${INPUT_AUTO_APPLY:-false}"

# -----------------------------------------------------------------------
# Build the PR URL from the GitHub Actions event context
# -----------------------------------------------------------------------
PR_NUMBER=$(jq -r '.pull_request.number // empty' "$GITHUB_EVENT_PATH" 2>/dev/null || true)
REPO="${GITHUB_REPOSITORY}"

if [[ -z "$PR_NUMBER" ]]; then
    echo "::error::This action must be triggered by a pull_request event."
    echo "status=error" >> "$GITHUB_OUTPUT"
    exit 1
fi

PR_URL="https://github.com/${REPO}/pull/${PR_NUMBER}"
echo "Reviewing PR: ${PR_URL}"

# -----------------------------------------------------------------------
# Fetch base branch so LocalBackend can compute the diff
# -----------------------------------------------------------------------
cd "${GITHUB_WORKSPACE}"
git fetch origin "${GITHUB_BASE_REF}" --depth=1 2>/dev/null || true

# -----------------------------------------------------------------------
# Run the pipeline via the external driver script
# -----------------------------------------------------------------------
export PR_URL
export AUTO_APPLY
python3 /app/run_pipeline.py
