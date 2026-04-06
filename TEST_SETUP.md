# Testing the GitHub Action End-to-End

This guide walks through creating a test repository that you can use to trigger and validate the PR Documentation Reviewer GitHub Action against real pull requests.

---

## 1. Create the Test Repo

```bash
mkdir pr-docs-reviewer-test && cd pr-docs-reviewer-test
git init
```

### Add some documentation

The action needs a `docs/` directory with real content to search and suggest updates against.

```bash
mkdir docs
```

Create `docs/getting-started.md`:

```markdown
# Getting Started

## Installation

pip install example-lib

## Configuration

The client accepts the following parameters:

- `base_url` (str): The base URL for API requests.
- `timeout` (int, default=30): Request timeout in seconds.

## Usage

from example_lib import Client

client = Client("https://api.example.com")
response = client.get("/users")
```

Create `docs/api-reference.md`:

```markdown
# API Reference

## Client

### `Client(base_url, timeout=30)`

Creates a new API client.

**Parameters:**

| Parameter | Type | Default | Description |
|-----------|------|---------|-------------|
| base_url  | str  | —       | The base URL |
| timeout   | int  | 30      | Timeout in seconds |

### `Client.get(path)`

Sends a GET request.

### `Client.post(path, data)`

Sends a POST request with a JSON body.

## Utilities

### `parse_response(response)`

Parses a raw API response into a structured result.
```

### Add a source file

Create `src/client.py`:

```python
class Client:
    def __init__(self, base_url: str, timeout: int = 30):
        self.base_url = base_url
        self.timeout = timeout

    def get(self, path: str):
        """Send a GET request."""
        ...

    def post(self, path: str, data: dict):
        """Send a POST request with JSON body."""
        ...
```

### Commit the baseline

```bash
git add .
git commit -m "Initial commit with docs and source"
```

### Push to GitHub

Create a new repo on GitHub (public or private), then:

```bash
git remote add origin git@github.com:<your-user>/pr-docs-reviewer-test.git
git branch -M main
git push -u origin main
```

---

## 2. Add the Workflow

Create `.github/workflows/pr-docs-review.yml`:

```yaml
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
      - uses: actions/checkout@v4
        with:
          fetch-depth: 0

      - name: PR Documentation Reviewer
        id: review
        uses: <your-user>/pr-docs-reviewer@main
        with:
          google_api_key: ${{ secrets.GOOGLE_API_KEY }}
          docs_path: "docs/"
          # auto_apply: "true"  # uncomment to test auto-PR creation
        env:
          GITHUB_TOKEN: ${{ secrets.GITHUB_TOKEN }}

      - name: Print results
        run: |
          echo "Status: ${{ steps.review.outputs.status }}"
          echo "Suggestions: ${{ steps.review.outputs.suggestions }}"
          echo "Doc PR URL: ${{ steps.review.outputs.doc_pr_url }}"
```

Replace `<your-user>/pr-docs-reviewer` with the actual location of the action. If the action lives in the same repo you're testing from, use a relative path instead (see section 5).

Commit and push:

```bash
git add .github/
git commit -m "Add PR docs review workflow"
git push
```

---

## 3. Configure Secrets

In the test repo on GitHub, go to **Settings > Secrets and variables > Actions** and add:

| Secret | Value |
|--------|-------|
| `GOOGLE_API_KEY` | Your Google AI API key ([get one here](https://aistudio.google.com/apikey)) |

`GITHUB_TOKEN` is provided automatically by GitHub Actions — you do not need to add it manually. The `permissions` block in the workflow grants it `contents: write` and `pull-requests: write` access.

---

## 4. Create a Test PR

Create a branch with a code change that should trigger a documentation update:

```bash
git checkout -b test/add-retry-logic
```

Edit `src/client.py` to add a new parameter:

```python
import time


class Client:
    def __init__(self, base_url: str, timeout: int = 30, max_retries: int = 3):
        self.base_url = base_url
        self.timeout = timeout
        self.max_retries = max_retries

    def get(self, path: str):
        """Send a GET request."""
        ...

    def post(self, path: str, data: dict):
        """Send a POST request with JSON body."""
        for attempt in range(self.max_retries):
            try:
                ...
            except ConnectionError:
                if attempt == self.max_retries - 1:
                    raise
                time.sleep(2 ** attempt)
```

Commit and push:

```bash
git add .
git commit -m "Add retry logic with max_retries parameter"
git push -u origin test/add-retry-logic
```

Open a PR on GitHub from `test/add-retry-logic` into `main`. The workflow will trigger automatically.

### What to expect

The action should:

1. Detect that `max_retries` was added to `Client.__init__`.
2. Find that `docs/getting-started.md` and `docs/api-reference.md` document `Client` but don't mention `max_retries`.
3. Suggest concrete doc updates (e.g., add `max_retries` to the parameter table).
4. Report `status=success` with a `suggestions` JSON array.

Check the **Actions** tab in your test repo to see the run output.

---

## 5. Testing Against a Local Copy of the Action

If you haven't published the action to a separate repo yet, you can reference it locally. There are two approaches:

### Option A: Monorepo — action and test content in the same repo

Copy the action source into the test repo (or just use the action repo itself as the test repo). Then reference the action with a local path:

```yaml
      - uses: ./
        with:
          google_api_key: ${{ secrets.GOOGLE_API_KEY }}
```

This uses the action definition from the repo's own root directory.

### Option B: Reference a branch of the action repo

If the action lives at `your-user/pr-docs-reviewer`, point the workflow at a specific branch:

```yaml
      - uses: your-user/pr-docs-reviewer@my-dev-branch
        with:
          google_api_key: ${{ secrets.GOOGLE_API_KEY }}
```

This is useful for testing action changes before merging to `main`.

---

## 6. Test Scenarios

Here are several PRs worth creating to exercise different action behaviors:

### Scenario 1: New parameter (should suggest doc updates)

Add a parameter to a documented function. Described in section 4 above.

**Expected:** `status=success`, suggestions to update parameter docs.

### Scenario 2: Internal refactor (should detect no doc impact)

```bash
git checkout -b test/internal-refactor
```

Rename a private variable or rearrange internal logic without changing the public API:

```python
# Change self.timeout to self._timeout internally
# Or extract a private helper method
```

**Expected:** `status=no_changes`.

### Scenario 3: Deleted function (should flag stale docs)

```bash
git checkout -b test/remove-parse-response
```

Delete `parse_response` from the source while `docs/api-reference.md` still references it.

**Expected:** `status=success`, suggestion to remove or update the `parse_response` section.

### Scenario 4: New file with no docs (should suggest new docs)

```bash
git checkout -b test/add-auth-module
```

Add a new `src/auth.py` with public classes/functions.

**Expected:** `status=success`, suggestion to add documentation for the new module.

### Scenario 5: Auto-apply mode

Uncomment `auto_apply: "true"` in the workflow, then open any PR that triggers suggestions.

**Expected:** The action creates a new branch and opens a follow-up PR with the doc changes applied. The `doc_pr_url` output contains the URL of the created PR.

---

## 7. Debugging Failures

### Check the Actions log

Go to the **Actions** tab in the test repo, click on the run, and expand the **PR Documentation Reviewer** step. The full pipeline output is printed there, including per-agent summaries.

### Common issues

| Symptom | Cause | Fix |
|---------|-------|-----|
| `status=error` with "must be triggered by a pull_request event" | Workflow triggered on push, not PR | Ensure the workflow uses `on: pull_request` |
| Action times out | Model is slow or rate-limited | Try a faster model: `model: "gemini-2.0-flash"` |
| `status=no_changes` when you expected suggestions | Docs directory not found or empty | Check `docs_path` matches your actual docs location |
| `auto_apply` doesn't create a PR | `GITHUB_TOKEN` lacks write permissions | Check the `permissions` block in the workflow |
| Docker build fails | Action repo has syntax errors | Check `docker build .` locally first |

### Test the Docker image locally

From the action repo (not the test repo):

```bash
docker build -t pr-docs-reviewer .
```

This validates the Dockerfile, dependency installation, and script copying without needing to push to GitHub.

---

## 8. Teardown

After testing, you can:

- Delete the test repo on GitHub.
- Or keep it around and re-use it by creating new branches/PRs whenever you want to test action changes.

The test PRs won't cost anything beyond the Gemini API calls during the action run.
