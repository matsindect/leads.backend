"""RemoteOK source adapter — fetches remote job listings via RSS.

Proves the RssFetcher pattern works end-to-end.  Job board posts are
a weaker signal than organic founder posts, so signal_strength is capped.

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

logger = structlog.get_logger()

_FEED_URL = "https://remoteok.com/remote-jobs.rss"

# Only keep entries whose title contains dev/engineering keywords
_DEV_ROLE_PATTERN = re.compile(
    r"\b(developer|engineer|dev|backend|frontend|fullstack|full.stack|"
    r"devops|sre|architect|programmer|software)\b",
    re.IGNORECASE,
)

# Extract company name from title prefix: "CompanyName - Role Title"
_COMPANY_FROM_TITLE = re.compile(r"^(.+?)\s*[-–—]\s+")


class RemoteOKAdapter:
    """Fetches remote dev job listings from RemoteOK RSS feed."""

    def __init__(self, fetcher: RssFetcher, settings: Settings) -> None:
        self._fetcher = fetcher
        self._settings = settings

    @property
    def name(self) -> str:
        return "remoteok"

    @property
    def poll_interval_seconds(self) -> int:
        return 600

    async def fetch_raw(self) -> list[dict[str, Any]]:
        """Fetch RSS feed and return raw entry dicts."""
        feed = await self._fetcher.fetch(_FEED_URL)
        logger.debug("remoteok_fetched", entries=len(feed.entries))
        # Convert RssEntry to dict for the SourceAdapter interface
        return [_entry_to_dict(e) for e in feed.entries]

    def normalize(self, raw: dict[str, Any]) -> CanonicalLead | None:
        """Convert an RSS entry to a CanonicalLead.

        Pure function — no I/O.  Filters to dev/engineering roles only.
        """
        title = raw.get("title", "")

        # Drop non-dev roles
        if not _DEV_ROLE_PATTERN.search(title):
            return None

        company_name = _extract_company(title)
        # Try to get domain from author email (e.g. jobs@company.com)
        company_domain = _domain_from_author(raw.get("author"))

        return CanonicalLead(
            source="remoteok",
            source_id=raw.get("id", raw.get("link", "")),
            url=raw.get("link", ""),
            title=title,
            body=raw.get("summary", "")[:5000],
            raw_payload=raw,
            signal_type=SignalType.HIRING,
            signal_strength=70,  # job board = weaker signal than direct posts
            company_name=company_name,
            company_domain=company_domain,
            person_name=raw.get("author"),
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
    """Extract domain from email-style author (e.g. 'jobs@acme.io')."""
    if not author or "@" not in author:
        return None
    parts = author.split("@")
    if len(parts) == 2:
        return parts[1].lower()
    return None
