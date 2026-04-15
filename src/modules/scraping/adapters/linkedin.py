"""LinkedIn adapter — fetches jobs and posts via RapidAPI.

Uses the Fresh LinkedIn Profile Data API to search for job postings
and organic LinkedIn posts.  No browser/Playwright needed — pure
HTTP API calls via HttpFetcher.

Satisfies ``domain.interfaces.SourceAdapter``.
"""

from __future__ import annotations

from datetime import datetime
from typing import Any

import structlog

from config import Settings
from domain.models import CanonicalLead, SignalType
from infrastructure.fetchers.http import HttpFetcher
from modules.scraping.signals import classify_signal, extract_stack

logger = structlog.get_logger()

_BASE_URL = "https://fresh-linkedin-profile-data.p.rapidapi.com"


class LinkedInAdapter:
    """Fetches LinkedIn jobs and posts via RapidAPI."""

    def __init__(self, fetcher: HttpFetcher, settings: Settings) -> None:
        self._fetcher = fetcher
        self._settings = settings
        self._headers = {
            "x-rapidapi-host": settings.linkedin_rapidapi_host,
            "x-rapidapi-key": settings.linkedin_rapidapi_key,
        }

    @property
    def name(self) -> str:
        return "linkedin"

    @property
    def poll_interval_seconds(self) -> int:
        return self._settings.linkedin_poll_interval_seconds

    async def fetch_raw(self) -> list[dict[str, Any]]:
        """Fetch jobs and posts from LinkedIn via RapidAPI."""
        all_items: list[dict[str, Any]] = []

        # Job search
        for query in self._settings.linkedin_job_queries:
            try:
                resp = await self._fetcher.get_json(
                    f"{_BASE_URL}/search-jobs",
                    params={"query": query, "page": "1"},
                    headers=self._headers,
                )
                jobs = resp.data.get("data", [])
                if isinstance(jobs, list):
                    for job in jobs:
                        job["_type"] = "job"
                        job["_query"] = query
                    all_items.extend(jobs)
                logger.debug(
                    "linkedin_jobs_fetched",
                    query=query, count=len(jobs),
                )
            except Exception:
                logger.warning(
                    "linkedin_job_search_failed",
                    query=query, exc_info=True,
                )

        # Post search
        for query in self._settings.linkedin_post_queries:
            try:
                resp = await self._fetcher.get_json(
                    f"{_BASE_URL}/search-posts",
                    params={"query": query, "page": "1"},
                    headers=self._headers,
                )
                posts = resp.data.get("data", [])
                if isinstance(posts, list):
                    for post in posts:
                        post["_type"] = "post"
                        post["_query"] = query
                    all_items.extend(posts)
                logger.debug(
                    "linkedin_posts_fetched",
                    query=query, count=len(posts),
                )
            except Exception:
                logger.warning(
                    "linkedin_post_search_failed",
                    query=query, exc_info=True,
                )

        return all_items

    def normalize(self, raw: dict[str, Any]) -> CanonicalLead | None:
        """Convert a LinkedIn API result to a CanonicalLead.

        Pure function — no I/O.
        """
        item_type = raw.get("_type", "job")
        if item_type == "post":
            return self._normalize_post(raw)
        return self._normalize_job(raw)

    def _normalize_job(self, raw: dict[str, Any]) -> CanonicalLead | None:
        """Normalize a job search result."""
        title = raw.get("job_title", "") or raw.get("title", "")
        if not title:
            return None

        company = raw.get("company_name", "") or raw.get("company", "")
        location = raw.get("location", "")
        url = raw.get("job_url", "") or raw.get("url", "") or ""

        combined = f"{title} {company} {raw.get('description', '')}"

        return CanonicalLead(
            source="linkedin",
            source_id=raw.get("job_id", "") or url,
            url=url,
            title=f"{company} — {title}" if company else title,
            body=raw.get("description", "")[:5000],
            raw_payload=raw,
            signal_type=SignalType.HIRING,
            signal_strength=80,
            company_name=company or None,
            location=location or None,
            stack_mentions=extract_stack(combined),
            posted_at=_parse_date(raw.get("posted_date")),
        )

    def _normalize_post(self, raw: dict[str, Any]) -> CanonicalLead | None:
        """Normalize a post search result."""
        text = raw.get("text", "") or raw.get("content", "")
        if not text:
            return None

        signal_type, signal_strength = classify_signal(text)
        if signal_type is None:
            return None

        author = raw.get("author_name", "") or raw.get("author", "")
        url = raw.get("post_url", "") or raw.get("url", "") or ""

        return CanonicalLead(
            source="linkedin",
            source_id=raw.get("post_id", "") or url,
            url=url,
            title=text[:120],
            body=text[:5000],
            raw_payload=raw,
            signal_type=signal_type,
            signal_strength=signal_strength,
            person_name=author or None,
            stack_mentions=extract_stack(text),
            posted_at=_parse_date(raw.get("posted_date")),
        )


def _parse_date(date_val: str | None) -> datetime | None:
    """Parse date from LinkedIn API — could be ISO string or relative."""
    if not date_val:
        return None
    try:
        return datetime.fromisoformat(
            date_val.replace("Z", "+00:00")
        )
    except (ValueError, AttributeError):
        return None
