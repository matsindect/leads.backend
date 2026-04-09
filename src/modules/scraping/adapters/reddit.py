"""Reddit source adapter — polls subreddit /new.rss feeds.

Uses Reddit's public Atom feeds because their JSON API blocks
datacenter/VPS IPs. RSS is delivered via the same fetch infrastructure
as other RSS sources.

Classifies posts via regex pattern matching against the signal taxonomy
and extracts technology stack mentions.  Posts with no signal match are
dropped.

Satisfies ``domain.interfaces.SourceAdapter``.
"""

from __future__ import annotations

import re
from typing import Any

import structlog

from config import Settings
from domain.models import CanonicalLead
from infrastructure.fetchers.rss import RssFetcher
from modules.scraping.signals import classify_signal, extract_domain, extract_stack

logger = structlog.get_logger()

# Reddit Atom feeds embed the post body as HTML inside <content>.
# Strip tags for plain-text classification but keep raw too.
_HTML_TAG = re.compile(r"<[^>]+>")

# Reddit author URLs are like "/user/username" — extract just the name.
_AUTHOR_NAME = re.compile(r"/user/([^/]+)")

# Permalinks look like /r/startups/comments/abc123/title/
_POST_ID = re.compile(r"/comments/([a-z0-9]+)/")


class RedditAdapter:
    """Fetches and normalizes leads from Reddit subreddit RSS feeds."""

    def __init__(self, fetcher: RssFetcher, settings: Settings) -> None:
        self._fetcher = fetcher
        self._settings = settings

    @property
    def name(self) -> str:
        return "reddit"

    @property
    def poll_interval_seconds(self) -> int:
        return self._settings.reddit_poll_interval_seconds

    async def fetch_raw(self) -> list[dict[str, Any]]:
        """Poll configured subreddits' /new.rss feeds and merge entries."""
        all_posts: list[dict[str, Any]] = []

        for subreddit in self._settings.reddit_subreddits:
            url = f"https://www.reddit.com/r/{subreddit}/new.rss"
            log = logger.bind(adapter=self.name, subreddit=subreddit)

            try:
                feed = await self._fetcher.fetch(url)
            except Exception:
                log.warning("reddit_fetch_failed", exc_info=True)
                continue

            for entry in feed.entries:
                all_posts.append({
                    "id": entry.id,
                    "title": entry.title,
                    "link": entry.link,
                    "summary": entry.summary,
                    "published_at": entry.published_at,
                    "author": entry.author,
                    "_subreddit": subreddit,
                })

            log.debug("subreddit_fetched", count=len(feed.entries))

        return all_posts

    def normalize(self, raw: dict[str, Any]) -> CanonicalLead | None:
        """Convert a raw Reddit RSS entry into a CanonicalLead.

        Pure function — no I/O.  Returns None if no signal matches.
        """
        title = raw.get("title", "")
        # RSS summary contains HTML — strip for classification, cap for storage
        summary_html = raw.get("summary", "")
        body = _HTML_TAG.sub("", summary_html).strip()
        combined_text = f"{title} {body}"

        signal_type, signal_strength = classify_signal(combined_text)
        if signal_type is None:
            return None

        stack_mentions = extract_stack(combined_text)
        company_domain = extract_domain(combined_text)

        return CanonicalLead(
            source="reddit",
            source_id=_extract_post_id(raw.get("link", "")) or raw.get("id", ""),
            url=raw.get("link", ""),
            title=title,
            body=body[:5000],
            raw_payload=raw,
            signal_type=signal_type,
            signal_strength=signal_strength,
            company_domain=company_domain,
            person_name=_extract_author_name(raw.get("author")),
            stack_mentions=stack_mentions,
            posted_at=raw.get("published_at"),
        )


def _extract_author_name(author: str | None) -> str | None:
    """Reddit RSS author is sometimes '/user/name' or just 'name'."""
    if not author:
        return None
    match = _AUTHOR_NAME.search(author)
    return match.group(1) if match else author


def _extract_post_id(link: str) -> str | None:
    """Extract Reddit post ID from a permalink for stable dedup."""
    if not link:
        return None
    match = _POST_ID.search(link)
    return match.group(1) if match else None
