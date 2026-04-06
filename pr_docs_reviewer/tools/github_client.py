"""GitHub API client shared by all tools."""

import os
import time
import re
from functools import lru_cache

import httpx

_GITHUB_API_BASE = "https://api.github.com"
_MAX_RETRIES = 3
_BACKOFF_BASE = 2  # seconds


@lru_cache(maxsize=1)
def _get_token() -> str:
    token = os.environ.get("GITHUB_TOKEN", "")
    if not token:
        raise RuntimeError(
            "GITHUB_TOKEN environment variable is not set. "
            "Set it to a GitHub personal access token with 'repo' scope."
        )
    return token


def _headers(*, accept: str = "application/vnd.github+json") -> dict[str, str]:
    return {
        "Authorization": f"Bearer {_get_token()}",
        "Accept": accept,
        "X-GitHub-Api-Version": "2022-11-28",
    }


def github_get(
    path: str,
    *,
    params: dict | None = None,
    accept: str = "application/vnd.github+json",
    raw: bool = False,
) -> httpx.Response:
    """
    Make a GET request to the GitHub API with retry + backoff on rate limits.

    Args:
        path: API path (e.g., "/repos/owner/repo/pulls/42").
        params: Optional query parameters.
        accept: Accept header value.
        raw: If True, return raw response. If False, raise on HTTP errors.

    Returns:
        httpx.Response
    """
    url = f"{_GITHUB_API_BASE}{path}" if path.startswith("/") else path

    for attempt in range(_MAX_RETRIES):
        resp = httpx.get(
            url,
            headers=_headers(accept=accept),
            params=params,
            timeout=30.0,
        )

        if resp.status_code == 429 or (
            resp.status_code == 403 and "rate limit" in resp.text.lower()
        ):
            wait = _BACKOFF_BASE ** (attempt + 1)
            # Check for Retry-After header
            retry_after = resp.headers.get("Retry-After")
            if retry_after:
                wait = max(wait, int(retry_after))
            time.sleep(wait)
            continue

        if not raw:
            resp.raise_for_status()
        return resp

    # Final attempt — let it raise if it fails
    resp = httpx.get(
        url,
        headers=_headers(accept=accept),
        params=params,
        timeout=30.0,
    )
    if not raw:
        resp.raise_for_status()
    return resp


def github_post(
    path: str,
    *,
    json: dict,
    accept: str = "application/vnd.github+json",
) -> httpx.Response:
    """
    Make a POST request to the GitHub API with retry + backoff on rate limits.

    Args:
        path: API path (e.g., "/repos/owner/repo/git/refs").
        json: JSON body to send.
        accept: Accept header value.

    Returns:
        httpx.Response
    """
    url = f"{_GITHUB_API_BASE}{path}" if path.startswith("/") else path

    for attempt in range(_MAX_RETRIES):
        resp = httpx.post(
            url,
            headers=_headers(accept=accept),
            json=json,
            timeout=30.0,
        )

        if resp.status_code == 429 or (
            resp.status_code == 403 and "rate limit" in resp.text.lower()
        ):
            wait = _BACKOFF_BASE ** (attempt + 1)
            retry_after = resp.headers.get("Retry-After")
            if retry_after:
                wait = max(wait, int(retry_after))
            time.sleep(wait)
            continue

        return resp

    # Final attempt
    return httpx.post(
        url,
        headers=_headers(accept=accept),
        json=json,
        timeout=30.0,
    )


def github_put(
    path: str,
    *,
    json: dict,
    accept: str = "application/vnd.github+json",
) -> httpx.Response:
    """
    Make a PUT request to the GitHub API with retry + backoff on rate limits.

    Args:
        path: API path (e.g., "/repos/owner/repo/contents/path").
        json: JSON body to send.
        accept: Accept header value.

    Returns:
        httpx.Response
    """
    url = f"{_GITHUB_API_BASE}{path}" if path.startswith("/") else path

    for attempt in range(_MAX_RETRIES):
        resp = httpx.put(
            url,
            headers=_headers(accept=accept),
            json=json,
            timeout=30.0,
        )

        if resp.status_code == 429 or (
            resp.status_code == 403 and "rate limit" in resp.text.lower()
        ):
            wait = _BACKOFF_BASE ** (attempt + 1)
            retry_after = resp.headers.get("Retry-After")
            if retry_after:
                wait = max(wait, int(retry_after))
            time.sleep(wait)
            continue

        return resp

    # Final attempt
    return httpx.put(
        url,
        headers=_headers(accept=accept),
        json=json,
        timeout=30.0,
    )


def parse_pr_url(pr_url: str) -> tuple[str, str, int]:
    """
    Parse a GitHub PR URL into (owner, repo, pr_number).

    Accepts:
        https://github.com/owner/repo/pull/123
        https://github.com/owner/repo/pull/123/files
        github.com/owner/repo/pull/123

    Raises:
        ValueError: If the URL doesn't match the expected pattern.
    """
    pattern = r"(?:https?://)?github\.com/([^/]+)/([^/]+)/pull/(\d+)"
    match = re.match(pattern, pr_url)
    if not match:
        raise ValueError(
            f"Invalid GitHub PR URL: {pr_url!r}. "
            f"Expected format: https://github.com/owner/repo/pull/123"
        )
    owner, repo, number = match.groups()
    return owner, repo, int(number)
