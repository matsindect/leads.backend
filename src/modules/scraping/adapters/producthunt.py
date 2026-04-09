"""ProductHunt adapter — tracks daily product launches via RSS.

Polls the ProductHunt RSS feed.  Applies signal classification to
detect tool evaluations, expansion signals, and general interest.

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
from modules.scraping.signals import classify_signal, extract_stack

logger = structlog.get_logger()

_FEED_URL = "https://www.producthunt.com/feed"

# Extract product name from title: "ProductName — tagline"
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

    async def fetch_raw(self) -> list[dict[str, Any]]:
        """Fetch the ProductHunt RSS feed."""
        feed = await self._fetcher.fetch(_FEED_URL)
        logger.debug("producthunt_fetched", entries=len(feed.entries))
        return [_entry_to_dict(e) for e in feed.entries]

    def normalize(self, raw: dict[str, Any]) -> CanonicalLead | None:
        """Convert a PH entry to a CanonicalLead.

        Pure function — no I/O.
        """
        title = raw.get("title", "")
        body = raw.get("summary", "")
        combined = f"{title} {body}"

        # Try signal classification; default to GENERAL_INTEREST for launches
        signal_type, signal_strength = classify_signal(combined)
        if signal_type is None:
            signal_type = SignalType.GENERAL_INTEREST
            signal_strength = 40

        product_name = _extract_product_name(title)
        domain = _domain_from_link(raw.get("link", ""))

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
            company_domain=domain,
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


def _extract_product_name(title: str) -> str | None:
    match = _PRODUCT_FROM_TITLE.match(title)
    return match.group(1).strip() if match else None


def _domain_from_link(link: str) -> str | None:
    """Extract domain from the PH post link (the post links to PH, not the product)."""
    # PH RSS links are producthunt.com/posts/X — not useful for company domain
    # Return None; the enrichment pipeline's resolve_company stage handles this
    return None
