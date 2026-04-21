"""ProductHunt adapter — tracks daily product launches via RSS.

Polls the ProductHunt RSS feed.  Applies the request's classifier,
falling back to GENERAL_INTEREST when no pattern matches (so we keep
launches as weak leads by default).

Satisfies ``domain.interfaces.SourceAdapter``.
"""

from __future__ import annotations

import re
from typing import Any

import structlog

from api.schemas import AdapterParamSchema, ScrapeRequest
from config import Settings
from domain.models import CanonicalLead, SignalType
from infrastructure.fetchers.base import RssEntry
from infrastructure.fetchers.rss import RssFetcher
from modules.scraping.signals import SignalClassifier

logger = structlog.get_logger()

_FEED_URL = "https://www.producthunt.com/feed"

_PRODUCT_FROM_TITLE = re.compile(r"^(.+?)\s*[—–\-]\s+")


class ProductHuntAdapter:
    """Fetches product launches from ProductHunt RSS feed."""

    def __init__(self, fetcher: RssFetcher, settings: Settings) -> None:
        self._fetcher = fetcher
        self._settings = settings

    @property
    def name(self) -> str:
        return "producthunt"

    @property
    def poll_interval_seconds(self) -> int:
        return self._settings.producthunt_poll_interval_seconds

    @property
    def accepted_params(self) -> AdapterParamSchema:
        return AdapterParamSchema(
            name=self.name,
            notes=(
                "No source/query params (fixed feed URL). "
                "Pass signal_patterns/extract_keywords to tailor classification."
            ),
        )

    async def fetch_raw(self, params: ScrapeRequest) -> list[dict[str, Any]]:
        feed = await self._fetcher.fetch(_FEED_URL)
        logger.debug("producthunt_fetched", entries=len(feed.entries))
        return [_entry_to_dict(e) for e in feed.entries]

    def normalize(
        self, raw: dict[str, Any], classifier: SignalClassifier
    ) -> CanonicalLead | None:
        title = raw.get("title", "")
        body = raw.get("summary", "")
        combined = f"{title} {body}"

        signal_type, signal_strength = classifier.classify(combined)
        if signal_type is None:
            # ProductHunt launches default to GENERAL_INTEREST (weak lead)
            signal_type = SignalType.GENERAL_INTEREST
            signal_strength = 40

        product_name = _extract_product_name(title)

        return CanonicalLead(
            source="producthunt",
            source_id=raw.get("id", raw.get("link", "")),
            url=raw.get("link", ""),
            title=title,
            body=body[:5000],
            raw_payload=raw,
            signal_type=signal_type,
            signal_strength=signal_strength,
            company_name=product_name,
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


def _extract_product_name(title: str) -> str | None:
    match = _PRODUCT_FROM_TITLE.match(title)
    return match.group(1).strip() if match else None
