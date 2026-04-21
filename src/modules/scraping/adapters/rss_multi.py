"""Generic multi-feed RSS adapter — polls a user-configured list of feeds.

Per-request ``sources`` overrides ``rss_feed_urls``.  The request's
classifier drives both signal classification and keyword extraction.

Satisfies ``domain.interfaces.SourceAdapter``.
"""

from __future__ import annotations

from typing import Any

import structlog

from api.schemas import AdapterParamSchema, ScrapeRequest
from config import Settings
from domain.models import CanonicalLead, SignalType
from infrastructure.fetchers.base import RssEntry
from infrastructure.fetchers.rss import RssFetcher
from modules.scraping.signals import SignalClassifier, extract_domain

logger = structlog.get_logger()


class RssMultiAdapter:
    """Fetches and classifies entries from multiple RSS/Atom feeds."""

    def __init__(self, fetcher: RssFetcher, settings: Settings) -> None:
        self._fetcher = fetcher
        self._settings = settings

    @property
    def name(self) -> str:
        return "rss"

    @property
    def poll_interval_seconds(self) -> int:
        return self._settings.rss_multi_poll_interval_seconds

    @property
    def accepted_params(self) -> AdapterParamSchema:
        return AdapterParamSchema(
            name=self.name,
            uses_sources=True,
            default_sources=list(self._settings.rss_feed_urls),
            notes="sources = RSS/Atom feed URLs to poll.",
        )

    async def fetch_raw(self, params: ScrapeRequest) -> list[dict[str, Any]]:
        feeds = params.sources or self._settings.rss_feed_urls
        all_entries: list[dict[str, Any]] = []

        for url in feeds:
            feed = await self._fetcher.fetch(url)
            for entry in feed.entries:
                d = _entry_to_dict(entry)
                d["_feed_url"] = url
                all_entries.append(d)
            logger.debug("rss_feed_fetched", url=url, entries=len(feed.entries))

        return all_entries

    def normalize(
        self, raw: dict[str, Any], classifier: SignalClassifier
    ) -> CanonicalLead | None:
        title = raw.get("title", "")
        body = raw.get("summary", "")
        combined = f"{title} {body}"

        signal_type, signal_strength = classifier.classify(combined)
        if signal_type is None:
            # User-curated feeds are presumed relevant — keep as weak lead
            signal_type = SignalType.GENERAL_INTEREST
            signal_strength = 20

        return CanonicalLead(
            source="rss",
            source_id=raw.get("id", raw.get("link", "")),
            url=raw.get("link", ""),
            title=title,
            body=body[:5000],
            raw_payload=raw,
            signal_type=signal_type,
            signal_strength=signal_strength,
            company_domain=extract_domain(combined),
            person_name=raw.get("author"),
            keywords=classifier.extract_keywords(combined),
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
