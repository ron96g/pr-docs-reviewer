# PR Documentation Reviewer

A multi-agent system built on [Google ADK](https://github.com/google/adk-python) that automatically reviews pull requests for documentation impact. It analyzes code changes, finds relevant docs, and suggests concrete updates — with iterative quality review.

## How It Works

The system runs a four-stage pipeline using ADK's orchestration primitives:

```
SequentialAgent("pr_docs_pipeline")
├── pr_analyzer          — Fetches PR diff, extracts structured change summary
├── ParallelAgent("research_phase")
│   ├── code_mapper      — Reads source code, classifies semantic impact of changes
│   └── doc_finder       — Searches docs for sections affected by the changes
├── LoopAgent("refinement_loop", max_iterations=3)
│   ├── doc_writer       — Writes concrete doc update suggestions
│   └── quality_reviewer — Reviews suggestions for accuracy, completeness, tone
└── doc_applier          — Optionally commits changes and opens a PR
```

**No LLM tokens are spent on routing.** The pipeline flow is deterministic via `SequentialAgent` — each step gets full LLM reasoning power for its specialized task. The code mapper and doc finder run concurrently via `ParallelAgent`. The writer/reviewer loop implements ADK's Generator-Critic pattern with bounded iterations.

## Features

- **Structured diff parsing** — Raw diffs are parsed via `unidiff` into per-file summaries (change type, functions touched, hunk ranges). The LLM never sees raw diff text.
- **Semantic change classification** — Changes are classified by user-facing impact: breaking change, new feature, behavioral change, or cosmetic.
- **Multi-strategy doc search** — Finds docs by keyword, file reference, module name, and class name.
- **Quality-gated suggestions** — A separate reviewer agent checks accuracy, completeness, tone, specificity, and conciseness before suggestions are finalized.
- **Auto-apply mode** — Optionally creates a branch, commits doc changes, and opens a PR against the source PR's branch.
- **Dual backend** — Works via GitHub API (remote) or local filesystem (GitHub Actions CI).

## Quick Start

### Prerequisites

- Python 3.13+
- A [Google AI API key](https://aistudio.google.com/apikey) (for Gemini models)
- A [GitHub personal access token](https://github.com/settings/tokens) with `repo` scope

### Installation

```bash
git clone https://github.com/your-org/pr-docs-reviewer.git
cd pr-docs-reviewer
python -m venv .venv
source .venv/bin/activate
pip install -e .
```

### Configuration

Copy the example env file and fill in your keys:

```bash
cp .env.example .env
```

```bash
# Required
export GOOGLE_API_KEY=your-google-api-key
export GITHUB_TOKEN=your-github-token

# Optional: override models per agent (default: gemini-3-flash-preview)
export PR_DOCS_ANALYZER_MODEL=gemini-3-flash-preview
export PR_DOCS_MAPPER_MODEL=gemini-3-flash-preview
export PR_DOCS_FINDER_MODEL=gemini-3-flash-preview
export PR_DOCS_WRITER_MODEL=gemini-3-flash-preview
export PR_DOCS_REVIEWER_MODEL=gemini-3-flash-preview
export PR_DOCS_APPLIER_MODEL=gemini-3-flash-preview
```

### Run with ADK

```bash
# Interactive mode
adk run pr_docs_reviewer

# Web UI
adk web pr_docs_reviewer
```

When prompted, provide a PR URL:

```
Review this PR: https://github.com/owner/repo/pull/123
```

To enable auto-apply (creates a PR with the doc changes), set `auto_apply` in the session state before running.

## Use as a GitHub Action

Add this to your repository's workflow to automatically review PRs for documentation impact:

```yaml
# .github/workflows/pr-docs-review.yml
name: PR Documentation Review
on:
  pull_request:
    types: [opened, synchronize]

permissions:
  contents: write
  pull-requests: write

jobs:
  review-docs:
    runs-on: ubuntu-latest
    steps:
      - uses: your-org/pr-docs-reviewer@v1
        with:
          google_api_key: ${{ secrets.GOOGLE_API_KEY }}
          # Optional inputs:
          # auto_apply: "true"         # Create a PR with suggested changes
          # model: "gemini-3-flash-preview"  # Override the model for all agents
        env:
          GITHUB_TOKEN: ${{ secrets.GITHUB_TOKEN }}
```

### Action Inputs

| Input | Required | Default | Description |
|---|---|---|---|
| `google_api_key` | Yes | — | Google AI API key for Gemini models |
| `auto_apply` | No | `"false"` | When `"true"`, commits doc changes and opens a PR |
| `model` | No | `"gemini-3-flash-preview"` | Override the model used by all agents |
| `docs_path` | No | `"docs/"` | Path to the documentation directory in the repo |

### Action Outputs

| Output | Description |
|---|---|
| `suggestions` | JSON array of documentation update suggestions |
| `doc_pr_url` | URL of the created documentation PR (when `auto_apply` is enabled) |
| `status` | Result status: `success`, `no_changes`, or `error` |

## Architecture

### Data Flow

All inter-agent communication flows through `session.state` via ADK's `output_key` mechanism:

```
User Input (PR URL)
      │
      ▼
 pr_analyzer
      ├──▶ state["pr_summary"]       — Structured change summary
      ├──▶ state["changed_files"]    — List of changed files with metadata
      │
      ▼
 research_phase (parallel)
      ├── code_mapper
      │   └──▶ state["code_analysis"]        — Semantic change classifications
      ├── doc_finder
      │   └──▶ state["relevant_doc_sections"] — Doc sections needing updates
      │
      ▼
 refinement_loop (max 3 iterations)
      ├── doc_writer
      │   └──▶ state["doc_suggestions"]      — Concrete update suggestions
      ├── quality_reviewer
      │   └──▶ state["reviewer_feedback"]    — Critique or approval
      │        (escalate=True exits loop)
      │
      ▼
 doc_applier
      └──▶ state["apply_result"]             — PR URL or summary
```

### Backend System

The pipeline accesses repository data through a `RepoBackend` protocol with two implementations:

| Backend | When Used | How It Works |
|---|---|---|
| `GitHubAPIBackend` | Default, or `SOURCE_MODE=api` | All data fetched via GitHub REST API |
| `LocalBackend` | In GitHub Actions, or `SOURCE_MODE=local` | Reads from local checkout + event payload |

Auto-detection: when `GITHUB_ACTIONS=true` is set, the local backend is used automatically.

### Tools

| Tool | Used By | Purpose |
|---|---|---|
| `fetch_pr_diff` | pr_analyzer | Fetches PR metadata and diff, parses into structured summary |
| `read_file_contents` | code_mapper | Reads source files from the repo |
| `get_function_signatures` | code_mapper | Extracts function/class signatures via AST (Python) or regex |
| `search_docs_by_keyword` | doc_finder | Searches docs for function names, class names, concepts |
| `search_docs_by_file_reference` | doc_finder | Searches docs for references to changed source files |
| `read_doc_file` | doc_finder, doc_writer | Reads doc files and parses section structure |
| `apply_doc_updates` | doc_applier | Creates branch, commits changes, opens PR |

## Development

### Running Tests

```bash
# Unit tests (fast, no LLM calls)
pytest tests/ -v
```

### Project Structure

```
pr_docs_reviewer/
├── agent.py                    # Pipeline definition — all agent configs
├── __init__.py
└── tools/
    ├── __init__.py
    ├── backend.py              # RepoBackend protocol + factory
    ├── github_api_backend.py   # GitHub REST API implementation
    ├── local_backend.py        # Local filesystem implementation
    ├── github_client.py        # Shared HTTP client with retry/backoff
    ├── fetch_pr_diff.py        # PR diff fetcher + parser
    ├── read_file_contents.py   # File reader
    ├── get_function_signatures.py  # AST-based signature extractor
    ├── search_docs_by_keyword.py   # Keyword search
    ├── search_docs_by_file_reference.py  # File reference search
    ├── read_doc_file.py        # Doc file reader + section parser
    └── apply_doc_updates.py    # Branch/commit/PR creator
tests/
├── conftest.py
├── fixtures/
└── tools/                      # Unit tests for each tool
```

## License

MIT
