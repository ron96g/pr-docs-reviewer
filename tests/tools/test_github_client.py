"""Tests for github_post and github_put — retry + backoff on rate limits."""

from __future__ import annotations

from unittest.mock import patch, MagicMock

import pytest

from pr_docs_reviewer.tools.github_client import github_post, github_put


def _mock_response(*, status_code=200, json_data=None, text="", headers=None):
    resp = MagicMock()
    resp.status_code = status_code
    resp.text = text
    resp.headers = headers or {}
    if json_data is not None:
        resp.json.return_value = json_data
    return resp


@pytest.fixture(autouse=True)
def _fake_token():
    with patch("shared.tools.github_client._get_token", return_value="fake-token"):
        yield


# ---------------------------------------------------------------------------
# github_post
# ---------------------------------------------------------------------------


class TestGithubPost:

    @patch("shared.tools.github_client.httpx.post")
    def test_success(self, mock_post):
        mock_post.return_value = _mock_response(
            status_code=201,
            json_data={"id": 1},
        )

        resp = github_post("/repos/acme/widgets/git/refs", json={"ref": "refs/heads/new", "sha": "abc"})

        assert resp.status_code == 201
        mock_post.assert_called_once()
        call_kwargs = mock_post.call_args
        assert call_kwargs[1]["json"] == {"ref": "refs/heads/new", "sha": "abc"}

    @patch("shared.tools.github_client.time.sleep")
    @patch("shared.tools.github_client.httpx.post")
    def test_rate_limit_retry(self, mock_post, mock_sleep):
        rate_limit_resp = _mock_response(
            status_code=429,
            text="rate limit exceeded",
            headers={"Retry-After": "1"},
        )
        success_resp = _mock_response(status_code=201, json_data={"id": 1})
        mock_post.side_effect = [rate_limit_resp, success_resp]

        resp = github_post("/repos/acme/widgets/git/refs", json={"ref": "x", "sha": "y"})

        assert resp.status_code == 201
        assert mock_post.call_count == 2
        mock_sleep.assert_called_once()


# ---------------------------------------------------------------------------
# github_put
# ---------------------------------------------------------------------------


class TestGithubPut:

    @patch("shared.tools.github_client.httpx.put")
    def test_success(self, mock_put):
        mock_put.return_value = _mock_response(
            status_code=200,
            json_data={"content": {"sha": "new-sha"}},
        )

        resp = github_put(
            "/repos/acme/widgets/contents/docs/api.md",
            json={"message": "update", "content": "dGVzdA==", "branch": "docs/fix"},
        )

        assert resp.status_code == 200
        mock_put.assert_called_once()
        call_kwargs = mock_put.call_args
        assert call_kwargs[1]["json"]["branch"] == "docs/fix"

    @patch("shared.tools.github_client.time.sleep")
    @patch("shared.tools.github_client.httpx.put")
    def test_rate_limit_retry(self, mock_put, mock_sleep):
        rate_limit_resp = _mock_response(
            status_code=403,
            text="API rate limit exceeded",
            headers={},
        )
        success_resp = _mock_response(status_code=200, json_data={})
        mock_put.side_effect = [rate_limit_resp, success_resp]

        resp = github_put("/repos/acme/widgets/contents/f.md", json={"message": "x", "content": "y"})

        assert resp.status_code == 200
        assert mock_put.call_count == 2
        mock_sleep.assert_called_once()
