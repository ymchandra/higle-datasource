"""Small HTTP helper with exponential backoff, shared by all adapters."""

from __future__ import annotations

import logging
import time
from typing import Any, Callable

import requests

log = logging.getLogger(__name__)

RETRYABLE_STATUS = {429, 500, 502, 503, 504}
MAX_RETRIES = 3
BACKOFF_BASE_SEC = 1.0
MAX_RETRY_AFTER_SEC = 30.0
TIMEOUT_SEC = 30


class FetchError(Exception):
    """Upstream could not be fetched (network error, 4xx, exhausted retries)."""


class RateLimitedError(FetchError):
    """Upstream kept answering HTTP 429 after all retries."""


class InvalidResponseError(FetchError):
    """Upstream answered 200 but the body was not JSON (e.g. an HTML error page)."""


def _retry_after(response: requests.Response) -> float:
    try:
        return min(float(response.headers.get("Retry-After", 0)), MAX_RETRY_AFTER_SEC)
    except (TypeError, ValueError):
        return 0.0


def get_json(
    url: str,
    *,
    user_agent: str,
    params: dict[str, Any] | None = None,
    session: requests.Session | None = None,
    sleep: Callable[[float], None] = time.sleep,
    retries: int = MAX_RETRIES,
) -> Any:
    """GET ``url`` and return decoded JSON.

    Retries with exponential backoff (1s, 2s, 4s) on HTTP 429, 5xx and
    connection errors/timeouts. Raises ``FetchError`` subclasses on failure.
    """
    http = session or requests
    headers = {"User-Agent": user_agent, "Accept": "application/json"}
    last_status: int | None = None
    last_error = "unknown error"

    for attempt in range(retries + 1):
        if attempt:
            delay = BACKOFF_BASE_SEC * (2 ** (attempt - 1))
            sleep(max(delay, retry_after))
            log.debug("retry %d/%d for %s (%s)", attempt, retries, url, last_error)
        retry_after = 0.0
        try:
            response = http.get(url, headers=headers, params=params, timeout=TIMEOUT_SEC)
        except (requests.ConnectionError, requests.Timeout) as exc:
            last_status, last_error = None, f"{type(exc).__name__}: {exc}"
            continue

        if response.status_code in RETRYABLE_STATUS:
            last_status = response.status_code
            last_error = f"HTTP {response.status_code}"
            retry_after = _retry_after(response)
            continue
        if response.status_code >= 400:
            raise FetchError(f"HTTP {response.status_code} for {url}")
        try:
            return response.json()
        except ValueError as exc:
            raise InvalidResponseError(f"non-JSON response from {url}: {exc}") from exc

    if last_status == 429:
        raise RateLimitedError(f"rate limited (HTTP 429) for {url}")
    raise FetchError(f"giving up on {url} after {retries} retries: {last_error}")
