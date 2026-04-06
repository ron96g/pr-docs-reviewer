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

# Docker container actions mount the workspace as a different user.
# Git 2.35.2+ rejects operations on repos owned by other users unless
# the directory is explicitly marked as safe.
git config --global --add safe.directory "${GITHUB_WORKSPACE}"

# Set git identity for doc-applier commits
git config --global user.name "pr-docs-reviewer[bot]"
git config --global user.email "pr-docs-reviewer[bot]@users.noreply.github.com"

# Configure git + gh authentication for push / PR creation.
# actions/checkout sets an extraheader on the *host* runner, but Docker
# container actions don't inherit it.  Re-configure using GITHUB_TOKEN.
if [[ -n "${GITHUB_TOKEN:-}" ]]; then
    git config --global http.https://github.com/.extraheader \
        "Authorization: basic $(echo -n "x-access-token:${GITHUB_TOKEN}" | base64 -w0)"
    export GH_TOKEN="${GITHUB_TOKEN}"
fi

echo "Fetching base branch: ${GITHUB_BASE_REF}"
if ! git fetch origin "${GITHUB_BASE_REF}" --depth=1; then
    echo "::warning::Failed to fetch base branch '${GITHUB_BASE_REF}'. Diff computation may fail."
fi

# Debug: show git state so we can diagnose diff issues
echo "--- Git debug info ---"
echo "HEAD: $(git rev-parse HEAD)"
echo "Branches: $(git branch -a)"
echo "GITHUB_BASE_REF=${GITHUB_BASE_REF}"
echo "GITHUB_HEAD_REF=${GITHUB_HEAD_REF:-}"
echo "GITHUB_WORKSPACE=${GITHUB_WORKSPACE}"
echo "--- End debug info ---"

# -----------------------------------------------------------------------
# Run the pipeline via the external driver script
# -----------------------------------------------------------------------
export PR_URL
export AUTO_APPLY
python3 /app/run_pipeline.py
