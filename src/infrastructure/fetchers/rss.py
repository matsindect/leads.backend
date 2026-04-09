"""RssFetcher — thin wrapper around feedparser, backed by HttpFetcher.

Fetches the feed XML via HttpFetcher.get_text() (inheriting all retry/
backoff/timeout logic) then parses with feedparser in a thread executor.
Zero domain knowledge.
"""

from __future__ import annotations

import asyncio
from datetime import datetime
from email.utils import parsedate_to_datetime
from typing import Any

import feedparser  # type: ignore[import-untyped]
import structlog

from infrastructure.fetchers.base import RssEntry, RssFeed
from infrastructure.fetchers.http import HttpFetcher

logger = structlog.get_logger()


class RssFetcher:
    """Fetches and parses RSS/Atom feeds asynchronously."""

    def __init__(self, http: HttpFetcher) -> None:
        self._http = http

    async def fetch(self, url: str) -> RssFeed:
        """Fetch feed XML via HttpFetcher then parse with feedparser."""
        response = await self._http.get_text(url)
        raw_xml = response.data

        # feedparser is synchronous — run in thread to stay non-blocking
        parsed = await asyncio.to_thread(feedparser.parse, raw_xml)

        if parsed.bozo and not parsed.entries:
            logger.warning("malformed_feed", url=url, error=str(parsed.bozo_exception))
            return RssFeed(entries=[], feed_title="", feed_updated=None)

        entries = [_parse_entry(e) for e in parsed.entries]
        feed_title = parsed.feed.get("title", "")
        feed_updated = _parse_feed_date(parsed.feed)

        logger.debug("rss_parsed", url=url, entries=len(entries), title=feed_title)
        return RssFeed(entries=entries, feed_title=feed_title, feed_updated=feed_updated)


def _parse_entry(entry: Any) -> RssEntry:
    """Convert a feedparser entry to our RssEntry dataclass."""
    # id: prefer 'id', fall back to 'link'
    entry_id = entry.get("id") or entry.get("link", "")

    # published: prefer 'published', fall back to 'updated'
    published_at = _parse_date(
        entry.get("published") or entry.get("updated")
    )

    return RssEntry(
        id=entry_id,
        title=entry.get("title", ""),
        link=entry.get("link", ""),
        summary=entry.get("summary", ""),
        published_at=published_at,
        author=entry.get("author"),
        raw=dict(entry),
    )


def _parse_feed_date(feed: Any) -> datetime | None:
    return _parse_date(feed.get("updated") or feed.get("published"))


def _parse_date(date_str: str | None) -> datetime | None:
    """Try multiple date parsing strategies."""
    if not date_str:
        return None
    # feedparser sometimes pre-parses dates into struct_time via *_parsed keys,
    # but those are unreliable.  Parse the string directly.
    try:
        return parsedate_to_datetime(date_str)
    except (ValueError, TypeError):
        pass
    try:
        return datetime.fromisoformat(date_str.replace("Z", "+00:00"))
    except (ValueError, AttributeError):
        pass
    return None
