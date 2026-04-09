"""Shared fetch layer — three reusable fetchers for scraping adapters.

HttpFetcher  — JSON/text over HTTP with retry, backoff, rate-limit parsing.
RssFetcher   — RSS/Atom feeds via feedparser, backed by HttpFetcher.
BrowserFetcher — JS-rendered pages via Playwright (optional dependency).
"""

from infrastructure.fetchers.base import (
    FetcherError,
    FetchResponse,
    PermanentFetcherError,
    RateLimitedError,
    RateLimitInfo,
    TransientFetcherError,
)
from infrastructure.fetchers.http import HttpFetcher
from infrastructure.fetchers.rss import RssFetcher

__all__ = [
    "FetcherError",
    "FetchResponse",
    "HttpFetcher",
    "PermanentFetcherError",
    "RateLimitedError",
    "RateLimitInfo",
    "RssFetcher",
    "TransientFetcherError",
]
