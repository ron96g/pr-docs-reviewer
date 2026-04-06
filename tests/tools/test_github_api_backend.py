"""Tests for GitHubAPIBackend — each method delegates to github_get correctly."""

from __future__ import annotations

import base64
from unittest.mock import patch, MagicMock

import pytest

from pr_docs_reviewer.tools.backend import reset_backend
from pr_docs_reviewer.tools.github_api_backend import GitHubAPIBackend


@pytest.fixture(autouse=True)
def _clean_backend():
    reset_backend()
    yield
    reset_backend()


@pytest.fixture()
def backend() -> GitHubAPIBackend:
    b = GitHubAPIBackend()
    b.configure(owner="acme", repo="widgets", pr_number=42)
    return b


def _mock_response(*, json_data=None, text="", status_code=200):
    resp = MagicMock()
    resp.status_code = status_code
    resp.text = text
    if json_data is not None:
        resp.json.return_value = json_data
    return resp


# ---------------------------------------------------------------------------
# configure / _repo_path
# ---------------------------------------------------------------------------


class TestConfigure:

    def test_defaults_are_empty(self):
        b = GitHubAPIBackend()
        assert b.owner == ""
        assert b.repo == ""
        assert b.pr_number == 0

    def test_configure_sets_fields(self):
        b = GitHubAPIBackend()
        b.configure(owner="org", repo="proj", pr_number=7)
        assert b.owner == "org"
        assert b.repo == "proj"
        assert b.pr_number == 7
        assert b._repo_path == "org/proj"

    def test_reconfigure_overwrites(self):
        b = GitHubAPIBackend()
        b.configure(owner="a", repo="b", pr_number=1)
        b.configure(owner="x", repo="y", pr_number=99)
        assert b._repo_path == "x/y"
        assert b.pr_number == 99


# ---------------------------------------------------------------------------
# get_pr_metadata
# ---------------------------------------------------------------------------


class TestGetPrMetadata:

    @patch("shared.tools.github_api_backend.github_get")
    def test_returns_parsed_metadata(self, mock_get, backend):
        mock_get.return_value = _mock_response(json_data={
            "title": "Add feature X",
            "body": "Closes #10",
            "number": 42,
            "html_url": "https://github.com/acme/widgets/pull/42",
        })

        meta = backend.get_pr_metadata()

        mock_get.assert_called_once_with(
            "/repos/acme/widgets/pulls/42",
        )
        assert meta == {
            "title": "Add feature X",
            "body": "Closes #10",
            "number": 42,
            "html_url": "https://github.com/acme/widgets/pull/42",
            "repo": "acme/widgets",
        }

    @patch("shared.tools.github_api_backend.github_get")
    def test_handles_null_body(self, mock_get, backend):
        mock_get.return_value = _mock_response(json_data={
            "title": "Fix",
            "body": None,
            "number": 42,
            "html_url": "https://github.com/acme/widgets/pull/42",
        })

        meta = backend.get_pr_metadata()
        assert meta["body"] == ""

    @patch("shared.tools.github_api_backend.github_get")
    def test_handles_missing_keys(self, mock_get, backend):
        mock_get.return_value = _mock_response(json_data={})

        meta = backend.get_pr_metadata()
        assert meta["title"] == ""
        assert meta["body"] == ""
        assert meta["number"] == 42  # falls back to configured pr_number


# ---------------------------------------------------------------------------
# get_pr_diff
# ---------------------------------------------------------------------------


class TestGetPrDiff:

    @patch("shared.tools.github_api_backend.github_get")
    def test_returns_diff_text(self, mock_get, backend):
        diff_text = "diff --git a/foo.py b/foo.py\n+hello\n"
        mock_get.return_value = _mock_response(text=diff_text)

        result = backend.get_pr_diff()

        mock_get.assert_called_once_with(
            "/repos/acme/widgets/pulls/42",
            accept="application/vnd.github.v3.diff",
        )
        assert result == diff_text


# ---------------------------------------------------------------------------
# get_pr_files
# ---------------------------------------------------------------------------


class TestGetPrFiles:

    @patch("shared.tools.github_api_backend.github_get")
    def test_returns_file_list(self, mock_get, backend):
        files = [
            {"filename": "a.py", "status": "modified", "additions": 5, "deletions": 2},
            {"filename": "b.py", "status": "added", "additions": 10, "deletions": 0},
        ]
        mock_get.return_value = _mock_response(json_data=files)

        result = backend.get_pr_files()

        mock_get.assert_called_once_with(
            "/repos/acme/widgets/pulls/42/files",
        )
        assert result == files

    @patch("shared.tools.github_api_backend.github_get")
    def test_empty_pr(self, mock_get, backend):
        mock_get.return_value = _mock_response(json_data=[])
        assert backend.get_pr_files() == []


# ---------------------------------------------------------------------------
# read_file
# ---------------------------------------------------------------------------


class TestReadFile:

    @patch("shared.tools.github_api_backend.github_get")
    def test_decodes_base64_content(self, mock_get, backend):
        content_b64 = base64.b64encode(b"print('hello')").decode()
        mock_get.return_value = _mock_response(
            json_data={"content": content_b64},
            status_code=200,
        )

        result = backend.read_file("src/main.py", ref="abc123")

        mock_get.assert_called_once_with(
            "/repos/acme/widgets/contents/src/main.py",
            params={"ref": "abc123"},
            raw=True,
        )
        assert result == "print('hello')"

    @patch("shared.tools.github_api_backend.github_get")
    def test_default_ref_is_head(self, mock_get, backend):
        content_b64 = base64.b64encode(b"x = 1").decode()
        mock_get.return_value = _mock_response(
            json_data={"content": content_b64},
            status_code=200,
        )

        backend.read_file("f.py")

        mock_get.assert_called_once_with(
            "/repos/acme/widgets/contents/f.py",
            params={"ref": "HEAD"},
            raw=True,
        )

    @patch("shared.tools.github_api_backend.github_get")
    def test_file_not_found_raises(self, mock_get, backend):
        mock_get.return_value = _mock_response(status_code=404, text="Not Found")

        with pytest.raises(FileNotFoundError, match="File not found.*src/gone.py"):
            backend.read_file("src/gone.py")

    @patch("shared.tools.github_api_backend.github_get")
    def test_server_error_raises(self, mock_get, backend):
        mock_get.return_value = _mock_response(status_code=500, text="Internal error")

        with pytest.raises(RuntimeError, match="GitHub API returned 500"):
            backend.read_file("x.py")

    @patch("shared.tools.github_api_backend.github_get")
    def test_blob_fallback_for_large_files(self, mock_get, backend):
        """When content is None but git_url is present, fetch via Blobs API."""
        blob_content_b64 = base64.b64encode(b"large file content").decode()

        # First call: contents API returns no content, but has git_url
        contents_resp = _mock_response(
            json_data={
                "content": None,
                "git_url": "https://api.github.com/repos/acme/widgets/git/blobs/abc",
            },
            status_code=200,
        )
        # Second call: blobs API returns base64 content
        blob_resp = _mock_response(
            json_data={"content": blob_content_b64},
        )
        mock_get.side_effect = [contents_resp, blob_resp]

        result = backend.read_file("large.bin")

        assert result == "large file content"
        assert mock_get.call_count == 2
        # Second call should be to the git_url directly
        mock_get.assert_called_with(
            "https://api.github.com/repos/acme/widgets/git/blobs/abc",
        )


# ---------------------------------------------------------------------------
# search_code
# ---------------------------------------------------------------------------


class TestSearchCode:

    @patch("shared.tools.github_api_backend.github_get")
    def test_basic_search(self, mock_get, backend):
        mock_get.return_value = _mock_response(
            json_data={
                "items": [
                    {
                        "path": "docs/setup.md",
                        "text_matches": [
                            {"fragment": "Run pip install ..."},
                        ],
                    },
                ],
            },
            status_code=200,
        )

        results = backend.search_code("pip install")

        mock_get.assert_called_once_with(
            "/search/code",
            params={
                "q": "pip install repo:acme/widgets",
                "per_page": 20,
            },
            raw=True,
        )
        assert len(results) == 1
        assert results[0]["path"] == "docs/setup.md"
        assert results[0]["text_matches"] == [{"fragment": "Run pip install ..."}]

    @patch("shared.tools.github_api_backend.github_get")
    def test_search_with_path_prefix(self, mock_get, backend):
        mock_get.return_value = _mock_response(
            json_data={"items": []},
            status_code=200,
        )

        backend.search_code("keyword", path_prefix="src/")

        call_params = mock_get.call_args[1]["params"]
        assert "path:src/" in call_params["q"]

    @patch("shared.tools.github_api_backend.github_get")
    def test_search_with_per_page(self, mock_get, backend):
        mock_get.return_value = _mock_response(
            json_data={"items": []},
            status_code=200,
        )

        backend.search_code("x", per_page=5)

        call_params = mock_get.call_args[1]["params"]
        assert call_params["per_page"] == 5

    @patch("shared.tools.github_api_backend.github_get")
    def test_search_non_200_returns_empty(self, mock_get, backend):
        mock_get.return_value = _mock_response(status_code=422, text="Validation failed")

        results = backend.search_code("whatever")
        assert results == []

    @patch("shared.tools.github_api_backend.github_get")
    def test_search_no_text_matches(self, mock_get, backend):
        """Items without text_matches key still produce entries."""
        mock_get.return_value = _mock_response(
            json_data={
                "items": [
                    {"path": "README.md"},
                ],
            },
            status_code=200,
        )

        results = backend.search_code("readme")
        assert len(results) == 1
        assert results[0]["path"] == "README.md"
        assert results[0]["text_matches"] == []

    @patch("shared.tools.github_api_backend.github_get")
    def test_search_multiple_results(self, mock_get, backend):
        mock_get.return_value = _mock_response(
            json_data={
                "items": [
                    {"path": "a.py", "text_matches": [{"fragment": "match a"}]},
                    {"path": "b.py", "text_matches": [{"fragment": "match b"}]},
                    {"path": "c.py", "text_matches": []},
                ],
            },
            status_code=200,
        )

        results = backend.search_code("test")
        assert len(results) == 3
        assert [r["path"] for r in results] == ["a.py", "b.py", "c.py"]


# ---------------------------------------------------------------------------
# get_pr_head_ref
# ---------------------------------------------------------------------------


class TestGetPrHeadRef:

    @patch("shared.tools.github_api_backend.github_get")
    def test_returns_branch_and_sha(self, mock_get, backend):
        mock_get.return_value = _mock_response(json_data={
            "head": {
                "ref": "feature/add-retry",
                "sha": "abc123def456",
            },
        })

        ref, sha = backend.get_pr_head_ref()

        mock_get.assert_called_once_with("/repos/acme/widgets/pulls/42")
        assert ref == "feature/add-retry"
        assert sha == "abc123def456"


# ---------------------------------------------------------------------------
# create_branch
# ---------------------------------------------------------------------------


class TestCreateBranch:

    @patch("shared.tools.github_api_backend.github_post")
    def test_success(self, mock_post, backend):
        mock_post.return_value = _mock_response(status_code=201, json_data={})

        backend.create_branch("docs/update-for-pr-42", "abc123")

        mock_post.assert_called_once_with(
            "/repos/acme/widgets/git/refs",
            json={"ref": "refs/heads/docs/update-for-pr-42", "sha": "abc123"},
        )

    @patch("shared.tools.github_api_backend.github_post")
    def test_branch_already_exists_raises(self, mock_post, backend):
        mock_post.return_value = _mock_response(status_code=422, text="Reference already exists")

        with pytest.raises(RuntimeError, match="already exists"):
            backend.create_branch("docs/update-for-pr-42", "abc123")


# ---------------------------------------------------------------------------
# write_file
# ---------------------------------------------------------------------------


class TestWriteFile:

    @patch("shared.tools.github_api_backend.github_put")
    @patch("shared.tools.github_api_backend.github_get")
    def test_update_existing_file(self, mock_get, mock_put, backend):
        """When the file exists, write_file fetches blob SHA and includes it."""
        import base64

        mock_get.return_value = _mock_response(
            status_code=200,
            json_data={"sha": "existing-blob-sha"},
        )
        mock_put.return_value = _mock_response(status_code=200, json_data={})

        backend.write_file(
            path="docs/api.md",
            content="new content",
            message="docs: update api.md",
            branch="docs/update-for-pr-42",
        )

        # Verify GET was called to fetch blob SHA
        mock_get.assert_called_once_with(
            "/repos/acme/widgets/contents/docs/api.md",
            params={"ref": "docs/update-for-pr-42"},
            raw=True,
        )

        # Verify PUT was called with blob SHA
        put_body = mock_put.call_args[1]["json"]
        assert put_body["sha"] == "existing-blob-sha"
        assert put_body["message"] == "docs: update api.md"
        assert put_body["branch"] == "docs/update-for-pr-42"
        assert put_body["content"] == base64.b64encode(b"new content").decode()

    @patch("shared.tools.github_api_backend.github_put")
    @patch("shared.tools.github_api_backend.github_get")
    def test_create_new_file(self, mock_get, mock_put, backend):
        """When the file does not exist (404), write_file omits sha."""
        mock_get.return_value = _mock_response(status_code=404, text="Not Found")
        mock_put.return_value = _mock_response(status_code=201, json_data={})

        backend.write_file(
            path="docs/new.md",
            content="brand new",
            message="docs: add new.md",
            branch="docs/update-for-pr-42",
        )

        put_body = mock_put.call_args[1]["json"]
        assert "sha" not in put_body
        assert put_body["message"] == "docs: add new.md"


# ---------------------------------------------------------------------------
# create_pull_request
# ---------------------------------------------------------------------------


class TestCreatePullRequest:

    @patch("shared.tools.github_api_backend.github_post")
    def test_creates_pr_and_returns_dict(self, mock_post, backend):
        mock_post.return_value = _mock_response(
            status_code=201,
            json_data={
                "number": 456,
                "html_url": "https://github.com/acme/widgets/pull/456",
            },
        )

        result = backend.create_pull_request(
            title="docs: update for PR #42",
            body="Automated doc updates.",
            head="docs/update-for-pr-42",
            base="feature/add-retry",
        )

        mock_post.assert_called_once_with(
            "/repos/acme/widgets/pulls",
            json={
                "title": "docs: update for PR #42",
                "body": "Automated doc updates.",
                "head": "docs/update-for-pr-42",
                "base": "feature/add-retry",
            },
        )
        assert result == {
            "number": 456,
            "html_url": "https://github.com/acme/widgets/pull/456",
        }
