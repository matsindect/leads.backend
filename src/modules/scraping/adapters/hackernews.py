"""Hacker News source adapter — fetches from Algolia HN Search API.

Uses ``search_by_date`` to find recent stories mentioning hiring,
developer needs, or tool evaluations.  Applies the shared signal
classification from ``modules.scraping.signals``.

Satisfies ``domain.interfaces.SourceAdapter``.
"""

from __future__ import annotations

from datetime import datetime
from typing import Any

import structlog

from config import Settings
from domain.models import CanonicalLead
from infrastructure.fetchers.http import HttpFetcher
from modules.scraping.signals import classify_signal, extract_domain, extract_stack

logger = structlog.get_logger()

_ALGOLIA_URL = "https://hn.algolia.com/api/v1/search_by_date"


class HackerNewsAdapter:
    """Fetches and normalizes leads from Hacker News via Algolia API."""

    def __init__(self, fetcher: HttpFetcher, settings: Settings) -> None:
        self._fetcher = fetcher
        self._settings = settings

    @property
    def name(self) -> str:
        return "hackernews"

    @property
    def poll_interval_seconds(self) -> int:
        return self._settings.hn_poll_interval_seconds

    async def fetch_raw(self) -> list[dict[str, Any]]:
        """Search HN for stories with developer/hiring signals."""
        response = await self._fetcher.get_json(
            _ALGOLIA_URL,
            params={
                "query": "looking for developer OR need developer OR hiring developer",
                "tags": "story",
                "hitsPerPage": "25",
            },
        )

        hits = response.data.get("hits", [])
        logger.debug("hn_fetched", count=len(hits))
        return hits

    def normalize(self, raw: dict[str, Any]) -> CanonicalLead | None:
        """Convert a raw HN item into a CanonicalLead.

        Pure function — no I/O.  Returns None if no signal matches.
        """
        title = raw.get("title", "") or ""
        body = raw.get("story_text", "") or raw.get("comment_text", "") or ""
        combined = f"{title} {body}"

        signal_type, signal_strength = classify_signal(combined)
        if signal_type is None:
            return None

        stack_mentions = extract_stack(combined)
        company_domain = extract_domain(combined)
        posted_at = _parse_hn_timestamp(raw.get("created_at"))

        object_id = raw.get("objectID", "")
        url = raw.get("url") or f"https://news.ycombinator.com/item?id={object_id}"

        return CanonicalLead(
            source="hackernews",
            source_id=object_id,
            url=url,
            title=title,
            body=body[:5000],
            raw_payload=raw,
            signal_type=signal_type,
            signal_strength=signal_strength,
            company_domain=company_domain,
            person_name=raw.get("author"),
            stack_mentions=stack_mentions,
            posted_at=posted_at,
        )


def _parse_hn_timestamp(iso_str: str | None) -> datetime | None:
    """Parse HN Algolia ISO timestamp (e.g. '2024-04-07T12:00:00.000Z')."""
    if not iso_str:
        return None
    try:
        return datetime.fromisoformat(iso_str.replace("Z", "+00:00"))
    except ValueError:
        return None
