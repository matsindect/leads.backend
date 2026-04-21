"""RemoteOK source adapter — fetches remote job listings via RSS.

Filters entries to titles matching ``queries`` when provided; falls back
to a default dev/engineering keyword filter.  Signal is always HIRING
(strength 70 — job board is a weaker signal than direct founder posts).

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

_FEED_URL = "https://remoteok.com/remote-jobs.rss"

# Default role filter: only keep dev/engineering-flavored titles
_DEFAULT_ROLE_PATTERN = re.compile(
    r"\b(developer|engineer|dev|backend|frontend|fullstack|full.stack|"
    r"devops|sre|architect|programmer|software)\b",
    re.IGNORECASE,
)

# Extract company name from title prefix: "CompanyName - Role Title"
_COMPANY_FROM_TITLE = re.compile(r"^(.+?)\s*[-–—]\s+")


class RemoteOKAdapter:
    """Fetches remote job listings from RemoteOK RSS feed."""

    def __init__(self, fetcher: RssFetcher, settings: Settings) -> None:
        self._fetcher = fetcher
        self._settings = settings

    @property
    def name(self) -> str:
        return "remoteok"

    @property
    def poll_interval_seconds(self) -> int:
        return 600

    @property
    def accepted_params(self) -> AdapterParamSchema:
        return AdapterParamSchema(
            name=self.name,
            uses_queries=True,
            default_queries=["developer", "engineer", "devops"],
            notes=(
                "queries filter job titles (substring match, any-of). "
                "When omitted, a dev-role default pattern is used."
            ),
        )

    async def fetch_raw(self, params: ScrapeRequest) -> list[dict[str, Any]]:
        feed = await self._fetcher.fetch(_FEED_URL)
        logger.debug("remoteok_fetched", entries=len(feed.entries))
        entries = [_entry_to_dict(e) for e in feed.entries]
        # Stash the active queries on each entry so normalize() can filter.
        for entry in entries:
            entry["_queries"] = list(params.queries) if params.queries else None
        return entries

    def normalize(
        self, raw: dict[str, Any], classifier: SignalClassifier
    ) -> CanonicalLead | None:
        title = raw.get("title", "")

        # Role filter: either request queries (any-of, case-insensitive) or default pattern
        queries = raw.get("_queries")
        if queries:
            lower = title.lower()
            if not any(q.lower() in lower for q in queries):
                return None
        elif not _DEFAULT_ROLE_PATTERN.search(title):
            return None

        summary = raw.get("summary", "")
        combined = f"{title} {summary}"

        company_name = _extract_company(title)
        company_domain = _domain_from_author(raw.get("author"))

        return CanonicalLead(
            source="remoteok",
            source_id=raw.get("id", raw.get("link", "")),
            url=raw.get("link", ""),
            title=title,
            body=summary[:5000],
            raw_payload=raw,
            signal_type=SignalType.HIRING,
            signal_strength=70,
            company_name=company_name,
            company_domain=company_domain,
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


def _extract_company(title: str) -> str | None:
    match = _COMPANY_FROM_TITLE.match(title)
    return match.group(1).strip() if match else None


def _domain_from_author(author: str | None) -> str | None:
    if not author or "@" not in author:
        return None
    parts = author.split("@")
    if len(parts) == 2:
        return parts[1].lower()
    return None
