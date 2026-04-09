"""Funding sources adapter — tracks startup funding announcements via RSS.

Polls TechCrunch Fundraising, Crunchbase News, and user-configured feeds.
All entries map to ``SignalType.FUNDING`` with strength based on round stage.

Satisfies ``domain.interfaces.SourceAdapter``.
"""

from __future__ import annotations

import re
from typing import Any

import structlog

from config import Settings
from domain.models import CanonicalLead, SignalType
from infrastructure.fetchers.base import RssEntry
from infrastructure.fetchers.rss import RssFetcher
from modules.scraping.signals import extract_domain, extract_stack

logger = structlog.get_logger()

_SERIES_PATTERN = re.compile(r"\b(series\s+[a-e])\b", re.I)
_SEED_PATTERN = re.compile(r"\b(seed|pre.seed|angel)\b", re.I)
_AMOUNT_PATTERN = re.compile(r"\$(\d+(?:\.\d+)?)\s*([MBK])", re.I)

# "Acme raises...", "Acme secures...", "Acme closes..."
_COMPANY_FROM_TITLE = re.compile(
    r"^(.+?)\s+(?:raises?|secures?|closes?|announces?|gets?|lands?)\s", re.I
)


class FundingAdapter:
    """Fetches funding round announcements from RSS feeds."""

    def __init__(self, fetcher: RssFetcher, settings: Settings) -> None:
        self._fetcher = fetcher
        self._settings = settings

    @property
    def name(self) -> str:
        return "funding"

    @property
    def poll_interval_seconds(self) -> int:
        return self._settings.funding_poll_interval_seconds

    async def fetch_raw(self) -> list[dict[str, Any]]:
        """Fetch all configured funding feeds and merge entries."""
        all_entries: list[dict[str, Any]] = []

        for url in self._settings.funding_feed_urls:
            feed = await self._fetcher.fetch(url)
            for entry in feed.entries:
                d = _entry_to_dict(entry)
                d["_feed_url"] = url
                all_entries.append(d)
            logger.debug("funding_feed_fetched", url=url, entries=len(feed.entries))

        return all_entries

    def normalize(self, raw: dict[str, Any]) -> CanonicalLead | None:
        """Convert a funding RSS entry to a CanonicalLead.

        Pure function — no I/O.
        """
        title = raw.get("title", "")
        body = raw.get("summary", "")
        combined = f"{title} {body}"

        strength = _score_funding(title)
        company_name = _extract_company(title)

        return CanonicalLead(
            source="funding",
            source_id=raw.get("id", raw.get("link", "")),
            url=raw.get("link", ""),
            title=title,
            body=body[:5000],
            raw_payload=raw,
            signal_type=SignalType.FUNDING,
            signal_strength=strength,
            company_name=company_name,
            company_domain=extract_domain(combined),
            stack_mentions=extract_stack(combined),
            posted_at=raw.get("published_at"),
        )


def _entry_to_dict(entry: RssEntry) -> dict[str, Any]:
    return {
        "id": entry.id,
        "title": entry.title,
        "link": entry.link,
        "summary": entry.summary,
        "published_at": entry.published_at,
        "author": entry.author,
    }


def _score_funding(title: str) -> int:
    """Higher strength for later-stage or larger rounds."""
    if _SERIES_PATTERN.search(title):
        return 90
    if _SEED_PATTERN.search(title):
        return 80
    return 70


def _extract_company(title: str) -> str | None:
    match = _COMPANY_FROM_TITLE.match(title)
    return match.group(1).strip() if match else None
