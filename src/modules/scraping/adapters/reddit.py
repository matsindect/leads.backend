"""Reddit source adapter — polls subreddit /new.json endpoints.

Classifies posts via regex pattern matching against the signal taxonomy
and extracts technology stack mentions.  Posts with no signal match are
dropped.

Satisfies ``domain.interfaces.SourceAdapter``.
"""

from __future__ import annotations

from datetime import datetime, timezone
from typing import Any

import structlog

from config import Settings
from domain.models import CanonicalLead
from infrastructure.fetchers.http import HttpFetcher
from modules.scraping.signals import classify_signal, extract_domain, extract_stack

logger = structlog.get_logger()


class RedditAdapter:
    """Fetches and normalizes leads from Reddit subreddit new queues."""

    def __init__(self, fetcher: HttpFetcher, settings: Settings) -> None:
        self._fetcher = fetcher
        self._settings = settings

    @property
    def name(self) -> str:
        return "reddit"

    @property
    def poll_interval_seconds(self) -> int:
        return self._settings.reddit_poll_interval_seconds

    async def fetch_raw(self) -> list[dict[str, Any]]:
        """Poll configured subreddits' /new.json and merge results."""
        all_posts: list[dict[str, Any]] = []

        for subreddit in self._settings.reddit_subreddits:
            url = f"https://www.reddit.com/r/{subreddit}/new.json"
            log = logger.bind(adapter=self.name, subreddit=subreddit)

            response = await self._fetcher.get_json(
                url,
                params={"limit": str(self._settings.reddit_post_limit)},
            )

            children = response.data.get("data", {}).get("children", [])
            for child in children:
                post = child.get("data", {})
                post["_subreddit"] = subreddit
                all_posts.append(post)

            log.debug("subreddit_fetched", count=len(children))

        return all_posts

    def normalize(self, raw: dict[str, Any]) -> CanonicalLead | None:
        """Convert a raw Reddit post into a CanonicalLead.

        Pure function — no I/O.  Returns None if no signal matches.
        """
        title = raw.get("title", "")
        body = raw.get("selftext", "")
        combined_text = f"{title} {body}"

        signal_type, signal_strength = classify_signal(combined_text)
        if signal_type is None:
            return None

        stack_mentions = extract_stack(combined_text)
        company_domain = extract_domain(combined_text)
        posted_at = _parse_timestamp(raw.get("created_utc"))

        permalink = raw.get("permalink", "")
        url = f"https://www.reddit.com{permalink}" if permalink else raw.get("url", "")

        return CanonicalLead(
            source="reddit",
            source_id=raw.get("id", raw.get("name", "")),
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


def _parse_timestamp(utc_epoch: float | None) -> datetime | None:
    if utc_epoch is None:
        return None
    return datetime.fromtimestamp(utc_epoch, tz=timezone.utc)
