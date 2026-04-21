"""Reddit source adapter — polls subreddit /new.rss feeds.

Uses Reddit's public Atom feeds because their JSON API blocks
datacenter/VPS IPs. Accepts per-request `sources` (subreddits) and
`limit`, falling back to env config when omitted.

Satisfies ``domain.interfaces.SourceAdapter``.
"""

from __future__ import annotations

import re
from typing import Any

import structlog

from api.schemas import AdapterParamSchema, ScrapeRequest
from config import Settings
from domain.models import CanonicalLead
from infrastructure.fetchers.rss import RssFetcher
from modules.scraping.signals import SignalClassifier, extract_domain

logger = structlog.get_logger()

_HTML_TAG = re.compile(r"<[^>]+>")
_AUTHOR_NAME = re.compile(r"/user/([^/]+)")
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

    @property
    def accepted_params(self) -> AdapterParamSchema:
        return AdapterParamSchema(
            name=self.name,
            uses_sources=True,
            uses_limit=True,
            default_sources=list(self._settings.reddit_subreddits),
            default_limit=self._settings.reddit_post_limit,
            notes=(
                "sources = subreddit names (without /r/). "
                "limit = max posts kept per subreddit RSS feed."
            ),
        )

    async def fetch_raw(self, params: ScrapeRequest) -> list[dict[str, Any]]:
        subreddits = params.sources or self._settings.reddit_subreddits
        limit = params.limit or self._settings.reddit_post_limit
        all_posts: list[dict[str, Any]] = []

        for subreddit in subreddits:
            url = f"https://www.reddit.com/r/{subreddit}/new.rss"
            log = logger.bind(adapter=self.name, subreddit=subreddit)

            try:
                feed = await self._fetcher.fetch(url)
            except Exception:
                log.warning("reddit_fetch_failed", exc_info=True)
                continue

            for entry in feed.entries[:limit]:
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

    def normalize(
        self, raw: dict[str, Any], classifier: SignalClassifier
    ) -> CanonicalLead | None:
        title = raw.get("title", "")
        summary_html = raw.get("summary", "")
        body = _HTML_TAG.sub("", summary_html).strip()
        combined_text = f"{title} {body}"

        signal_type, signal_strength = classifier.classify(combined_text)
        if signal_type is None:
            return None

        keywords = classifier.extract_keywords(combined_text)
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
            keywords=keywords,
            posted_at=raw.get("published_at"),
        )


def _extract_author_name(author: str | None) -> str | None:
    if not author:
        return None
    match = _AUTHOR_NAME.search(author)
    return match.group(1) if match else author


def _extract_post_id(link: str) -> str | None:
    if not link:
        return None
    match = _POST_ID.search(link)
    return match.group(1) if match else None
