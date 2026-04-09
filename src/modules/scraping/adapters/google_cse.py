"""Google Custom Search Engine adapter — searches for leads via Google CSE API.

Free tier: 100 queries/day.  An in-memory daily counter prevents
budget overruns.  Each call to fetch_raw() runs one query per
configured search term.

Satisfies ``domain.interfaces.SourceAdapter``.
"""

from __future__ import annotations

from datetime import date
from typing import Any
from urllib.parse import urlparse

import structlog

from config import Settings
from domain.models import CanonicalLead
from infrastructure.fetchers.http import HttpFetcher
from modules.scraping.signals import classify_signal, extract_domain, extract_stack

logger = structlog.get_logger()

_CSE_API_URL = "https://www.googleapis.com/customsearch/v1"


class _DailyBudget:
    """In-memory daily query counter with automatic date rollover."""

    def __init__(self, max_queries: int) -> None:
        self._max = max_queries
        self._count = 0
        self._date = date.today()

    def can_query(self) -> bool:
        self._maybe_reset()
        return self._count < self._max

    def record_query(self) -> None:
        self._maybe_reset()
        self._count += 1

    @property
    def remaining(self) -> int:
        self._maybe_reset()
        return max(0, self._max - self._count)

    def _maybe_reset(self) -> None:
        today = date.today()
        if today != self._date:
            self._count = 0
            self._date = today


class GoogleCSEAdapter:
    """Fetches search results from Google Custom Search Engine API."""

    def __init__(self, fetcher: HttpFetcher, settings: Settings) -> None:
        self._fetcher = fetcher
        self._settings = settings
        self._budget = _DailyBudget(settings.google_cse_daily_query_budget)

    @property
    def name(self) -> str:
        return "google_cse"

    @property
    def poll_interval_seconds(self) -> int:
        return self._settings.google_cse_poll_interval_seconds

    async def fetch_raw(self) -> list[dict[str, Any]]:
        """Run configured search queries against the CSE API."""
        all_results: list[dict[str, Any]] = []

        for query in self._settings.google_cse_queries:
            if not self._budget.can_query():
                logger.warning(
                    "google_cse_budget_exhausted",
                    remaining=self._budget.remaining,
                )
                break

            response = await self._fetcher.get_json(
                _CSE_API_URL,
                params={
                    "key": self._settings.google_cse_api_key,
                    "cx": self._settings.google_cse_engine_id,
                    "q": query,
                    "num": "10",
                },
            )
            self._budget.record_query()

            items = response.data.get("items", [])
            for item in items:
                item["_query"] = query
            all_results.extend(items)
            logger.debug("google_cse_query", query=query, results=len(items))

        return all_results

    def normalize(self, raw: dict[str, Any]) -> CanonicalLead | None:
        """Convert a CSE result to a CanonicalLead.

        Pure function — no I/O.  Drops results with no signal match.
        """
        title = raw.get("title", "")
        snippet = raw.get("snippet", "")
        combined = f"{title} {snippet}"
        link = raw.get("link", "")

        signal_type, signal_strength = classify_signal(combined)
        if signal_type is None:
            return None

        # Extract domain from the result URL itself
        domain = _domain_from_url(link)

        return CanonicalLead(
            source="google_cse",
            source_id=link,
            url=link,
            title=title,
            body=snippet,
            raw_payload=raw,
            signal_type=signal_type,
            signal_strength=signal_strength,
            company_domain=domain or extract_domain(combined),
            stack_mentions=extract_stack(combined),
        )


def _domain_from_url(url: str) -> str | None:
    """Extract the domain from a URL, excluding common platforms."""
    try:
        parsed = urlparse(url)
        host = parsed.hostname or ""
    except ValueError:
        return None
    # Skip common platforms — their domain isn't the company's
    skip = {"reddit.com", "news.ycombinator.com", "twitter.com", "x.com", "github.com"}
    bare = host.removeprefix("www.")
    if bare in skip:
        return None
    return bare or None
