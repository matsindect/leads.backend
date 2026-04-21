"""Google Custom Search Engine adapter.

Per-request ``queries`` overrides ``google_cse_queries``.  ``limit`` maps
to ``num`` (max results per query, CSE caps at 10).  Daily query budget
is enforced to protect the free tier (100 queries/day).

Satisfies ``domain.interfaces.SourceAdapter``.
"""

from __future__ import annotations

from datetime import date
from typing import Any
from urllib.parse import urlparse

import structlog

from api.schemas import AdapterParamSchema, ScrapeRequest
from config import Settings
from domain.models import CanonicalLead
from infrastructure.fetchers.http import HttpFetcher
from modules.scraping.signals import SignalClassifier, extract_domain

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

    @property
    def accepted_params(self) -> AdapterParamSchema:
        return AdapterParamSchema(
            name=self.name,
            uses_queries=True,
            uses_limit=True,
            default_queries=list(self._settings.google_cse_queries),
            default_limit=10,
            requires_api_key=True,
            notes="limit maps to num (max 10 per query). Daily budget enforced in-memory.",
        )

    async def fetch_raw(self, params: ScrapeRequest) -> list[dict[str, Any]]:
        queries = params.queries or self._settings.google_cse_queries
        num = str(min(params.limit or 10, 10))
        all_results: list[dict[str, Any]] = []

        for query in queries:
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
                    "num": num,
                },
            )
            self._budget.record_query()

            items = response.data.get("items", [])
            for item in items:
                item["_query"] = query
            all_results.extend(items)
            logger.debug("google_cse_query", query=query, results=len(items))

        return all_results

    def normalize(
        self, raw: dict[str, Any], classifier: SignalClassifier
    ) -> CanonicalLead | None:
        title = raw.get("title", "")
        snippet = raw.get("snippet", "")
        combined = f"{title} {snippet}"
        link = raw.get("link", "")

        signal_type, signal_strength = classifier.classify(combined)
        if signal_type is None:
            return None

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
            keywords=classifier.extract_keywords(combined),
        )


def _domain_from_url(url: str) -> str | None:
    try:
        parsed = urlparse(url)
        host = parsed.hostname or ""
    except ValueError:
        return None
    skip = {"reddit.com", "news.ycombinator.com", "twitter.com", "x.com", "github.com"}
    bare = host.removeprefix("www.")
    if bare in skip:
        return None
    return bare or None
