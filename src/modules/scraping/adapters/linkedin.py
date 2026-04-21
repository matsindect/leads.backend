"""LinkedIn adapter — fetches jobs and posts via Fresh LinkedIn Profile Data (RapidAPI).

Uses two POST endpoints:
    POST /search-jobs   body: {"keywords": "...", "page": 1}
    POST /search-posts  body: {"search_keywords": "...", "sort_by": "Latest", ...}

Satisfies ``domain.interfaces.SourceAdapter``.
"""

from __future__ import annotations

from datetime import datetime
from typing import Any

import structlog

from api.schemas import AdapterParamSchema, ScrapeRequest
from config import Settings
from domain.models import CanonicalLead, SignalType
from infrastructure.fetchers.http import HttpFetcher
from modules.scraping.signals import SignalClassifier

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

    @property
    def accepted_params(self) -> AdapterParamSchema:
        return AdapterParamSchema(
            name=self.name,
            uses_queries=True,
            supported_filters=["job_queries", "post_queries"],
            default_queries=list(self._settings.linkedin_post_queries),
            requires_api_key=True,
            notes=(
                "queries are used as post_queries by default. "
                "filters.job_queries and filters.post_queries override independently. "
                "Requires LEADS_LINKEDIN_RAPIDAPI_KEY."
            ),
        )

    async def fetch_raw(self, params: ScrapeRequest) -> list[dict[str, Any]]:
        """Fetch jobs and posts from LinkedIn via RapidAPI."""
        filters = params.filters or {}
        job_queries = (
            filters.get("job_queries")
            or self._settings.linkedin_job_queries
        )
        post_queries = (
            filters.get("post_queries")
            or params.queries
            or self._settings.linkedin_post_queries
        )
        all_items: list[dict[str, Any]] = []

        # Job search — POST {"keywords": "...", "page": 1}
        for query in job_queries:
            try:
                resp = await self._fetcher.post_json(
                    f"{_BASE_URL}/search-jobs",
                    json_body={"keywords": query, "page": 1},
                    headers=self._headers,
                )
                jobs = resp.data.get("data") or []
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

        # Post search — POST with richer payload
        for query in post_queries:
            try:
                resp = await self._fetcher.post_json(
                    f"{_BASE_URL}/search-posts",
                    json_body={
                        "search_keywords": query,
                        "sort_by": "Latest",
                        "date_posted": "",
                        "content_type": "",
                        "from_member": [],
                        "from_company": [],
                        "mentioning_member": [],
                        "mentioning_company": [],
                        "author_company": [],
                        "author_industry": [],
                        "author_keyword": "",
                        "page": 1,
                    },
                    headers=self._headers,
                )
                posts = resp.data.get("data") or []
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

    def normalize(
        self, raw: dict[str, Any], classifier: SignalClassifier
    ) -> CanonicalLead | None:
        """Convert a LinkedIn API result to a CanonicalLead.

        Pure function — no I/O.
        """
        item_type = raw.get("_type", "job")
        if item_type == "post":
            return self._normalize_post(raw, classifier)
        return self._normalize_job(raw, classifier)

    def _normalize_job(
        self, raw: dict[str, Any], classifier: SignalClassifier
    ) -> CanonicalLead | None:
        """Normalize a /search-jobs result."""
        title = (
            raw.get("job_title")
            or raw.get("title")
            or raw.get("position")
            or ""
        )
        if not title:
            return None

        company = raw.get("company") or raw.get("company_name") or ""
        location = raw.get("location") or raw.get("job_location") or ""
        url = (
            raw.get("job_url")
            or raw.get("url")
            or raw.get("apply_url")
            or ""
        )
        description = raw.get("description") or raw.get("job_description") or ""

        combined = f"{title} {company} {description}"

        return CanonicalLead(
            source="linkedin",
            source_id=str(raw.get("job_id") or raw.get("id") or url),
            url=url,
            title=f"{company} — {title}" if company else title,
            body=description[:5000],
            raw_payload=raw,
            signal_type=SignalType.HIRING,
            signal_strength=80,
            company_name=company or None,
            location=location or None,
            keywords=classifier.extract_keywords(combined),
            posted_at=_parse_date(
                raw.get("posted_date") or raw.get("posted_at") or raw.get("posted")
            ),
        )

    def _normalize_post(
        self, raw: dict[str, Any], classifier: SignalClassifier
    ) -> CanonicalLead | None:
        """Normalize a /search-posts result."""
        text = raw.get("text") or raw.get("content") or ""
        if not text:
            return None

        signal_type, signal_strength = classifier.classify(text)
        if signal_type is None:
            return None

        author = raw.get("poster_name") or raw.get("author_name") or ""
        author_title = raw.get("poster_title") or ""
        url = raw.get("post_url") or raw.get("url") or ""

        return CanonicalLead(
            source="linkedin",
            source_id=str(raw.get("post_id") or url),
            url=url,
            title=text[:120],
            body=text[:5000],
            raw_payload=raw,
            signal_type=signal_type,
            signal_strength=signal_strength,
            person_name=author or None,
            person_role=author_title or None,
            keywords=classifier.extract_keywords(text),
            posted_at=_parse_date(raw.get("posted") or raw.get("posted_at")),
        )


def _parse_date(date_val: str | None) -> datetime | None:
    """Parse date from LinkedIn API — ISO string or 'YYYY-MM-DD HH:MM:SS.fff'."""
    if not date_val:
        return None
    # Try ISO-with-Z first
    try:
        return datetime.fromisoformat(date_val.replace("Z", "+00:00"))
    except (ValueError, AttributeError):
        pass
    # Try LinkedIn's "2026-04-21 06:56:57.000" format
    try:
        return datetime.strptime(date_val, "%Y-%m-%d %H:%M:%S.%f")
    except (ValueError, TypeError):
        pass
    try:
        return datetime.strptime(date_val, "%Y-%m-%d %H:%M:%S")
    except (ValueError, TypeError):
        return None
