"""GitHub REST API backend — fetches all data via api.github.com.

This wraps the existing ``github_client.py`` helpers and implements
the :class:`RepoBackend` protocol.
"""

from __future__ import annotations

import base64

from .github_client import github_get, github_post, github_put


class GitHubAPIBackend:
    """RepoBackend implementation that talks to the GitHub REST API.

    The owner/repo/pr_number are set after construction via
    :meth:`configure`, typically by ``fetch_pr_diff`` once it has
    parsed the PR URL.
    """

    def __init__(self) -> None:
        self.owner: str = ""
        self.repo: str = ""
        self.pr_number: int = 0

    def configure(self, *, owner: str, repo: str, pr_number: int) -> None:
        """Set the PR context for subsequent API calls."""
        self.owner = owner
        self.repo = repo
        self.pr_number = pr_number

    @property
    def _repo_path(self) -> str:
        return f"{self.owner}/{self.repo}"

    # -- PR-level data -------------------------------------------------------

    def get_pr_metadata(self) -> dict:
        resp = github_get(f"/repos/{self._repo_path}/pulls/{self.pr_number}")
        meta = resp.json()
        return {
            "title": meta.get("title", ""),
            "body": meta.get("body", "") or "",
            "number": meta.get("number", self.pr_number),
            "html_url": meta.get("html_url", ""),
            "repo": self._repo_path,
        }

    def get_pr_diff(self) -> str:
        resp = github_get(
            f"/repos/{self._repo_path}/pulls/{self.pr_number}",
            accept="application/vnd.github.v3.diff",
        )
        return resp.text

    def get_pr_files(self) -> list[dict]:
        resp = github_get(
            f"/repos/{self._repo_path}/pulls/{self.pr_number}/files",
        )
        return resp.json()

    # -- File-level data -----------------------------------------------------

    def read_file(self, path: str, ref: str = "HEAD") -> str:
        resp = github_get(
            f"/repos/{self._repo_path}/contents/{path}",
            params={"ref": ref},
            raw=True,
        )

        if resp.status_code == 404:
            raise FileNotFoundError(
                f"File not found: {path} at ref {ref}"
            )

        if resp.status_code != 200:
            raise RuntimeError(
                f"GitHub API returned {resp.status_code}: {resp.text[:200]}"
            )

        data = resp.json()

        # Handle large files via Git Blobs API
        if data.get("content") is None and data.get("git_url"):
            blob_resp = github_get(data["git_url"])
            blob = blob_resp.json()
            return base64.b64decode(blob["content"]).decode(
                "utf-8", errors="replace"
            )

        return base64.b64decode(data["content"]).decode(
            "utf-8", errors="replace"
        )

    # -- Search --------------------------------------------------------------

    def search_code(
        self,
        query: str,
        path_prefix: str = "",
        per_page: int = 20,
    ) -> list[dict]:
        full_query = f"{query} repo:{self._repo_path}"
        if path_prefix:
            full_query += f" path:{path_prefix}"

        resp = github_get(
            "/search/code",
            params={"q": full_query, "per_page": per_page},
            raw=True,
        )

        if resp.status_code != 200:
            return []

        data = resp.json()
        results: list[dict] = []

        for item in data.get("items", []):
            text_matches = item.get("text_matches", [])
            results.append({
                "path": item["path"],
                "text_matches": [
                    {"fragment": tm.get("fragment", "")}
                    for tm in text_matches
                ],
            })

        return results

    # -- Write operations (for doc applier) ---------------------------------

    def get_pr_head_ref(self) -> tuple[str, str]:
        resp = github_get(f"/repos/{self._repo_path}/pulls/{self.pr_number}")
        data = resp.json()
        return data["head"]["ref"], data["head"]["sha"]

    def create_branch(self, branch_name: str, sha: str) -> None:
        resp = github_post(
            f"/repos/{self._repo_path}/git/refs",
            json={"ref": f"refs/heads/{branch_name}", "sha": sha},
        )
        if resp.status_code == 422:
            raise RuntimeError(f"Branch {branch_name!r} already exists")
        resp.raise_for_status()

    def write_file(
        self, path: str, content: str, message: str, branch: str,
    ) -> None:
        # Get current blob SHA (needed for updates)
        get_resp = github_get(
            f"/repos/{self._repo_path}/contents/{path}",
            params={"ref": branch},
            raw=True,
        )
        blob_sha = (
            get_resp.json().get("sha")
            if get_resp.status_code == 200
            else None
        )

        body: dict = {
            "message": message,
            "content": base64.b64encode(content.encode()).decode(),
            "branch": branch,
        }
        if blob_sha:
            body["sha"] = blob_sha

        resp = github_put(
            f"/repos/{self._repo_path}/contents/{path}", json=body,
        )
        resp.raise_for_status()

    def create_pull_request(
        self, title: str, body: str, head: str, base: str,
    ) -> dict:
        resp = github_post(
            f"/repos/{self._repo_path}/pulls",
            json={"title": title, "body": body, "head": head, "base": base},
        )
        resp.raise_for_status()
        data = resp.json()
        return {"number": data["number"], "html_url": data["html_url"]}
