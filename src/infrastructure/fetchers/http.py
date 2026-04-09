"""HttpFetcher — shared HTTP fetch with retry, backoff, and rate-limit parsing.

Wraps httpx.AsyncClient with tenacity retries.  Every adapter that speaks
HTTP should use this instead of calling httpx directly.  Zero domain knowledge.
"""

from __future__ import annotations

import time
from collections.abc import Mapping
from datetime import UTC, datetime
from typing import Any

import httpx
import structlog

from infrastructure.fetchers.base import (
    FetchResponse,
    PermanentFetcherError,
    RateLimitedError,
    RateLimitInfo,
    TransientFetcherError,
)

logger = structlog.get_logger()

# Header names vary across APIs — we check all common variants
_REMAINING_HEADERS = ("x-ratelimit-remaining", "ratelimit-remaining")
_RESET_HEADERS = ("x-ratelimit-reset", "ratelimit-reset")
_LIMIT_HEADERS = ("x-ratelimit-limit", "ratelimit-limit")


class HttpFetcher:
    """Reusable HTTP fetcher with retry, backoff, and rate-limit parsing.

    Injected with a shared httpx.AsyncClient so connection pooling is
    managed once at the container level, not per-fetcher.
    """

    def __init__(
        self,
        client: httpx.AsyncClient,
        *,
        user_agent: str,
        default_timeout_sec: float = 30.0,
        max_retries: int = 3,
    ) -> None:
        self._client = client
        self._user_agent = user_agent
        self._default_timeout = default_timeout_sec
        self._max_retries = max_retries

    async def get_json(
        self,
        url: str,
        *,
        params: dict[str, Any] | None = None,
        headers: dict[str, str] | None = None,
        timeout_sec: float | None = None,
    ) -> FetchResponse:
        """Fetch URL and parse response as JSON."""
        return await self._fetch(
            url, parse="json", params=params,
            headers=headers, timeout_sec=timeout_sec,
        )

    async def get_text(
        self,
        url: str,
        *,
        params: dict[str, Any] | None = None,
        headers: dict[str, str] | None = None,
        timeout_sec: float | None = None,
    ) -> FetchResponse:
        """Fetch URL and return raw text body."""
        return await self._fetch(
            url, parse="text", params=params,
            headers=headers, timeout_sec=timeout_sec,
        )

    async def post_json(
        self,
        url: str,
        *,
        json_body: dict[str, Any] | None = None,
        params: dict[str, Any] | None = None,
        headers: dict[str, str] | None = None,
        timeout_sec: float | None = None,
    ) -> FetchResponse:
        """POST to URL with a JSON body and parse response as JSON."""
        return await self._fetch(
            url, method="POST", parse="json",
            json_body=json_body, params=params, headers=headers, timeout_sec=timeout_sec,
        )

    async def _fetch(
        self,
        url: str,
        *,
        method: str = "GET",
        parse: str,
        json_body: dict[str, Any] | None = None,
        params: dict[str, Any] | None,
        headers: dict[str, str] | None,
        timeout_sec: float | None,
    ) -> FetchResponse:
        """Core fetch with tenacity retry wrapper.

        We build the retry decorator dynamically so max_retries is
        configurable per-instance rather than per-class.
        """
        attempt = 0
        last_exc: Exception | None = None
        timeout = timeout_sec or self._default_timeout

        merged_headers = {"User-Agent": self._user_agent}
        if headers:
            merged_headers.update(headers)

        # Manual retry loop (cleaner than dynamic tenacity decorator for per-instance config)
        wait_times = [1.0, 4.0, 16.0]  # exponential: 1s, 4s, 16s

        for attempt in range(1, self._max_retries + 1):
            start = time.monotonic()
            log = logger.bind(url=url, attempt=attempt)

            try:
                if method == "POST":
                    response = await self._client.post(
                        url,
                        json=json_body,
                        params=params,
                        headers=merged_headers,
                        timeout=timeout,
                    )
                else:
                    response = await self._client.get(
                        url,
                        params=params,
                        headers=merged_headers,
                        timeout=timeout,
                    )
                duration_ms = int((time.monotonic() - start) * 1000)
                log.debug("http_response", status=response.status_code, duration_ms=duration_ms)

                rate_limit = _parse_rate_limit(response.headers)

                # 429 — honor Retry-After, then retry
                if response.status_code == 429:
                    retry_after = _parse_retry_after(response.headers)
                    if attempt < self._max_retries:
                        wait = min(retry_after or wait_times[attempt - 1], 60.0)
                        log.warning("rate_limited", retry_after=wait)
                        import asyncio
                        await asyncio.sleep(wait)
                        continue
                    raise RateLimitedError(
                        f"429 after {attempt} attempts: {url}",
                        url=url,
                        retry_after=retry_after,
                    )

                # 5xx — transient, retry
                if response.status_code >= 500:
                    if attempt < self._max_retries:
                        wait = wait_times[attempt - 1]
                        log.warning("server_error_retrying", status=response.status_code, wait=wait)
                        import asyncio
                        await asyncio.sleep(wait)
                        continue
                    raise TransientFetcherError(
                        f"{response.status_code} after {attempt} attempts: {url}",
                        url=url,
                        status=response.status_code,
                    )

                # 4xx (not 429) — permanent failure
                if response.status_code >= 400:
                    raise PermanentFetcherError(
                        f"{response.status_code}: {url}",
                        url=url,
                        status=response.status_code,
                    )

                # Success
                data: Any = (
                    response.json() if parse == "json" else response.text
                )

                return FetchResponse(
                    status=response.status_code,
                    data=data,
                    headers=dict(response.headers),
                    rate_limit=rate_limit,
                    duration_ms=duration_ms,
                )

            except (httpx.TransportError, httpx.TimeoutException) as exc:
                duration_ms = int((time.monotonic() - start) * 1000)
                last_exc = exc
                if attempt < self._max_retries:
                    wait = wait_times[attempt - 1]
                    log.warning("network_error_retrying", error=str(exc), wait=wait)
                    import asyncio
                    await asyncio.sleep(wait)
                    continue

        # Exhausted all retries on network errors
        raise TransientFetcherError(
            f"Network error after {self._max_retries} attempts: {url} — {last_exc}",
            url=url,
        )


def _parse_rate_limit(headers: Mapping[str, str]) -> RateLimitInfo | None:
    """Extract rate-limit info from common header variants."""
    remaining = _first_int(headers, _REMAINING_HEADERS)
    limit = _first_int(headers, _LIMIT_HEADERS)
    reset_at = _first_reset(headers, _RESET_HEADERS)

    if remaining is None and limit is None and reset_at is None:
        return None

    return RateLimitInfo(remaining=remaining, reset_at=reset_at, limit=limit)


def _first_int(headers: Mapping[str, str], names: tuple[str, ...]) -> int | None:
    """Return the first header value parseable as int, or None."""
    for name in names:
        val = headers.get(name)
        if val is not None:
            try:
                return int(val)
            except ValueError:
                continue
    return None


def _first_reset(headers: Mapping[str, str], names: tuple[str, ...]) -> datetime | None:
    """Parse reset header as Unix timestamp."""
    for name in names:
        val = headers.get(name)
        if val is not None:
            try:
                return datetime.fromtimestamp(int(val), tz=UTC)
            except (ValueError, OSError):
                continue
    return None


def _parse_retry_after(headers: Mapping[str, str]) -> float | None:
    """Parse Retry-After header as seconds."""
    val = headers.get("retry-after")
    if val is None:
        return None
    try:
        return float(val)
    except ValueError:
        return None
