"""Shared types for the fetch layer.

These dataclasses and exceptions are pure infrastructure —
they have zero knowledge of leads, signals, or domain concepts.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime
from typing import Any, Mapping


# ---------------------------------------------------------------------------
# Response types
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class RateLimitInfo:
    """Parsed rate-limit headers from an HTTP response."""

    remaining: int | None = None
    reset_at: datetime | None = None
    limit: int | None = None


@dataclass(frozen=True)
class FetchResponse:
    """Uniform response from any fetcher method."""

    status: int
    data: Any  # parsed JSON dict/list, raw text, or None
    headers: Mapping[str, str] = field(default_factory=dict)
    rate_limit: RateLimitInfo | None = None
    duration_ms: int = 0


# ---------------------------------------------------------------------------
# RSS types
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class RssEntry:
    """One entry from an RSS/Atom feed."""

    id: str
    title: str
    link: str
    summary: str
    published_at: datetime | None
    author: str | None
    raw: dict[str, Any] = field(default_factory=dict)


@dataclass(frozen=True)
class RssFeed:
    """Parsed RSS/Atom feed."""

    entries: list[RssEntry]
    feed_title: str
    feed_updated: datetime | None = None


# ---------------------------------------------------------------------------
# Exceptions — typed hierarchy so callers can catch at the right level
# ---------------------------------------------------------------------------


class FetcherError(Exception):
    """Base exception for all fetcher errors."""

    def __init__(self, message: str, *, url: str = "", status: int | None = None) -> None:
        super().__init__(message)
        self.url = url
        self.status = status


class TransientFetcherError(FetcherError):
    """Retryable error — retries exhausted without success (5xx, network errors)."""


class PermanentFetcherError(FetcherError):
    """Non-retryable error — 4xx (except 429), bad response, etc."""


class RateLimitedError(FetcherError):
    """429 after retries — caller should back off at a higher level."""

    def __init__(
        self, message: str, *, url: str = "", retry_after: float | None = None
    ) -> None:
        super().__init__(message, url=url, status=429)
        self.retry_after = retry_after


class BrowserTimeoutError(FetcherError):
    """Playwright page timed out."""
