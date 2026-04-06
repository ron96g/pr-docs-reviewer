"""Local filesystem backend — reads data from a checked-out repo.

Designed for GitHub Actions where the repository is already cloned
and PR context is available via environment variables and the event
payload JSON file.

Required GitHub Actions environment variables:
    GITHUB_EVENT_PATH   — path to the JSON event payload
    GITHUB_REPOSITORY   — "owner/repo"
    GITHUB_BASE_REF     — base branch name (e.g., "main")
    GITHUB_HEAD_REF     — head branch name (or "" for push events)

Optional:
    GITHUB_WORKSPACE    — repo root (defaults to cwd)
"""

from __future__ import annotations

import json
import os
import re
import subprocess
from pathlib import Path


class LocalBackend:
    """RepoBackend implementation that reads from the local filesystem.

    Args:
        repo_root: Path to the repository root.  Defaults to
            ``GITHUB_WORKSPACE`` or the current working directory.
    """

    def __init__(self, repo_root: str | Path | None = None):
        if repo_root is not None:
            self._root = Path(repo_root)
        else:
            self._root = Path(
                os.environ.get("GITHUB_WORKSPACE", os.getcwd())
            )

    @property
    def repo_root(self) -> Path:
        return self._root

    # -- helpers -------------------------------------------------------------

    def _load_event_payload(self) -> dict:
        """Load the GitHub Actions event payload JSON."""
        event_path = os.environ.get("GITHUB_EVENT_PATH", "")
        if not event_path or not Path(event_path).exists():
            raise RuntimeError(
                "GITHUB_EVENT_PATH is not set or the file does not exist. "
                "This backend requires a GitHub Actions PR event payload."
            )
        with open(event_path, encoding="utf-8") as f:
            return json.load(f)

    def _git(self, *args: str) -> str:
        """Run a git command in the repo root and return stdout."""
        result = subprocess.run(
            ["git", *args],
            capture_output=True,
            text=True,
            cwd=self._root,
        )
        if result.returncode != 0:
            raise RuntimeError(
                f"git {' '.join(args)} failed (rc={result.returncode}): "
                f"{result.stderr.strip()}"
            )
        return result.stdout

    def _base_ref(self) -> str:
        """Return the merge-base ref string for git diff."""
        base = os.environ.get("GITHUB_BASE_REF", "")
        if not base:
            raise RuntimeError(
                "GITHUB_BASE_REF is not set.  This backend requires "
                "a GitHub Actions pull_request event."
            )
        return f"origin/{base}"

    def _head_ref(self) -> str:
        """Return the head ref string for git diff."""
        head = os.environ.get("GITHUB_HEAD_REF", "")
        return head if head else "HEAD"

    # -- PR-level data -------------------------------------------------------

    def get_pr_metadata(self) -> dict:
        event = self._load_event_payload()
        pr = event.get("pull_request", {})
        repo = os.environ.get("GITHUB_REPOSITORY", "")
        return {
            "title": pr.get("title", ""),
            "body": pr.get("body", "") or "",
            "number": pr.get("number", 0),
            "html_url": pr.get("html_url", ""),
            "repo": repo,
        }

    def get_pr_diff(self) -> str:
        base = self._base_ref()
        head = self._head_ref()
        return self._git("diff", f"{base}...{head}")

    def get_pr_files(self) -> list[dict]:
        """Parse ``git diff --numstat`` into a file list.

        Returns a list of dicts compatible with the GitHub API shape:
        ``{filename, status, additions, deletions}``.
        """
        base = self._base_ref()
        head = self._head_ref()

        # --numstat gives: additions<TAB>deletions<TAB>filename
        numstat = self._git("diff", "--numstat", f"{base}...{head}")
        # --name-status gives: status<TAB>filename
        name_status = self._git("diff", "--name-status", f"{base}...{head}")

        status_map: dict[str, str] = {}
        for line in name_status.strip().splitlines():
            if not line.strip():
                continue
            parts = line.split("\t", 1)
            if len(parts) == 2:
                raw_status, fname = parts
                # Map git status letters to GitHub API status strings
                status_map[fname] = {
                    "A": "added",
                    "D": "deleted",
                    "M": "modified",
                }.get(raw_status[0], "modified")

        files: list[dict] = []
        for line in numstat.strip().splitlines():
            if not line.strip():
                continue
            parts = line.split("\t", 2)
            if len(parts) == 3:
                adds, dels, fname = parts
                files.append({
                    "filename": fname,
                    "status": status_map.get(fname, "modified"),
                    "additions": int(adds) if adds != "-" else 0,
                    "deletions": int(dels) if dels != "-" else 0,
                })

        return files

    # -- File-level data -----------------------------------------------------

    def read_file(self, path: str, ref: str = "HEAD") -> str:
        file_path = self._root / path
        if not file_path.exists():
            raise FileNotFoundError(f"File not found: {path}")
        return file_path.read_text(encoding="utf-8", errors="replace")

    # -- Search --------------------------------------------------------------

    def search_code(
        self,
        query: str,
        path_prefix: str = "",
        per_page: int = 20,
    ) -> list[dict]:
        search_dir = self._root / path_prefix if path_prefix else self._root
        if not search_dir.is_dir():
            return []

        pattern = re.compile(re.escape(query), re.IGNORECASE)
        results: list[dict] = []
        seen_paths: set[str] = set()

        for file_path in sorted(search_dir.rglob("*")):
            if not file_path.is_file():
                continue

            # Skip hidden directories (.git, etc.)
            if any(part.startswith(".") for part in file_path.parts):
                continue

            try:
                content = file_path.read_text(encoding="utf-8", errors="replace")
            except Exception:
                continue

            text_matches: list[dict] = []
            for line in content.splitlines():
                if pattern.search(line):
                    text_matches.append({
                        "fragment": line.strip()[:200],
                    })

            if text_matches:
                rel_path = str(file_path.relative_to(self._root))
                if rel_path not in seen_paths:
                    seen_paths.add(rel_path)
                    results.append({
                        "path": rel_path,
                        "text_matches": text_matches[:5],  # cap fragments per file
                    })

            if len(results) >= per_page:
                break

        return results

    # -- Write operations (for doc applier) ---------------------------------

    def get_pr_head_ref(self) -> tuple[str, str]:
        event = self._load_event_payload()
        pr = event["pull_request"]
        return pr["head"]["ref"], pr["head"]["sha"]

    def create_branch(self, branch_name: str, sha: str) -> None:
        subprocess.run(
            ["git", "checkout", "-b", branch_name, sha],
            cwd=self._root,
            check=True,
            capture_output=True,
        )

    def write_file(
        self, path: str, content: str, message: str, branch: str,
    ) -> None:
        file_path = self._root / path
        file_path.parent.mkdir(parents=True, exist_ok=True)
        file_path.write_text(content, encoding="utf-8")
        subprocess.run(
            ["git", "add", path],
            cwd=self._root,
            check=True,
            capture_output=True,
        )
        subprocess.run(
            ["git", "commit", "-m", message],
            cwd=self._root,
            check=True,
            capture_output=True,
        )

    def create_pull_request(
        self, title: str, body: str, head: str, base: str,
    ) -> dict:
        # Push the branch first
        subprocess.run(
            ["git", "push", "origin", head],
            cwd=self._root,
            check=True,
            capture_output=True,
        )
        # Use GitHub CLI (available in all GitHub Actions runners)
        result = subprocess.run(
            [
                "gh", "pr", "create",
                "--title", title,
                "--body", body,
                "--head", head,
                "--base", base,
            ],
            cwd=self._root,
            check=True,
            capture_output=True,
            text=True,
        )
        # gh pr create prints the PR URL to stdout
        pr_url = result.stdout.strip()
        # Extract PR number from URL
        pr_number = int(pr_url.rstrip("/").split("/")[-1])
        return {"number": pr_number, "html_url": pr_url}
