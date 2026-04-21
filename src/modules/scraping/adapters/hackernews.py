"""HackerNews source adapter — Algolia search.

Accepts per-request `queries` (joined with OR) and `limit`, falling
back to a developer-hiring-focused default.

Satisfies ``domain.interfaces.SourceAdapter``.
"""

from __future__ import annotations

from datetime import datetime
from typing import Any

import structlog

from api.schemas import AdapterParamSchema, ScrapeRequest
from config import Settings
from domain.models import CanonicalLead
from infrastructure.fetchers.http import HttpFetcher
from modules.scraping.signals import SignalClassifier, extract_domain

logger = structlog.get_logger()

_ALGOLIA_URL = "https://hn.algolia.com/api/v1/search_by_date"
_DEFAULT_QUERY = "looking for developer OR need developer OR hiring developer"
_DEFAULT_LIMIT = 25


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

    @property
    def accepted_params(self) -> AdapterParamSchema:
        return AdapterParamSchema(
            name=self.name,
            uses_queries=True,
            uses_limit=True,
            default_queries=[_DEFAULT_QUERY],
            default_limit=_DEFAULT_LIMIT,
            notes="queries are joined with OR into a single Algolia query. limit = hitsPerPage.",
        )

    async def fetch_raw(self, params: ScrapeRequest) -> list[dict[str, Any]]:
        query = " OR ".join(params.queries) if params.queries else _DEFAULT_QUERY
        limit = params.limit or _DEFAULT_LIMIT

        response = await self._fetcher.get_json(
            _ALGOLIA_URL,
            params={
                "query": query,
                "tags": "story",
                "hitsPerPage": str(limit),
            },
        )

        hits = response.data.get("hits", [])
        logger.debug("hn_fetched", count=len(hits))
        return hits

    def normalize(
        self, raw: dict[str, Any], classifier: SignalClassifier
    ) -> CanonicalLead | None:
        title = raw.get("title", "") or ""
        body = raw.get("story_text", "") or raw.get("comment_text", "") or ""
        combined = f"{title} {body}"

        signal_type, signal_strength = classifier.classify(combined)
        if signal_type is None:
            return None

        keywords = classifier.extract_keywords(combined)
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
            keywords=keywords,
            posted_at=posted_at,
        )


def _parse_hn_timestamp(iso_str: str | None) -> datetime | None:
    if not iso_str:
        return None
    try:
        return datetime.fromisoformat(iso_str.replace("Z", "+00:00"))
    except ValueError:
        return None
