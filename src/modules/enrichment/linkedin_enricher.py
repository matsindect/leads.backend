"""LinkedIn job enrichment — GET /get-job-details on RapidAPI.

Standalone utility: deep-fetches one LinkedIn job posting given its URL.
Not yet wired into the enrichment pipeline — integration is part of the
enrichment pipeline design (see docs/enrichment-pipeline-design.md).
"""

from __future__ import annotations

from typing import Any

import structlog

from config import Settings
from infrastructure.fetchers.http import HttpFetcher

logger = structlog.get_logger()

_BASE_URL = "https://fresh-linkedin-profile-data.p.rapidapi.com"


class LinkedInJobEnricher:
    """Fetch deep details for a LinkedIn job posting."""

    def __init__(self, fetcher: HttpFetcher, settings: Settings) -> None:
        self._fetcher = fetcher
        self._settings = settings
        self._headers = {
            "x-rapidapi-host": settings.linkedin_rapidapi_host,
            "x-rapidapi-key": settings.linkedin_rapidapi_key,
        }

    async def fetch_job_details(
        self,
        job_url: str,
        *,
        include_skills: bool | None = None,
        include_hiring_team: bool | None = None,
    ) -> dict[str, Any] | None:
        """GET /get-job-details for *job_url*.

        When ``include_skills`` or ``include_hiring_team`` are None, the
        settings defaults (``linkedin_enrich_include_skills`` /
        ``linkedin_enrich_include_hiring_team``) are used.

        Returns the ``data`` block from the response, or ``None`` when the
        response shape is unexpected. Transport / HTTP errors propagate —
        callers decide whether to retry or skip.
        """
        skills = (
            include_skills
            if include_skills is not None
            else self._settings.linkedin_enrich_include_skills
        )
        team = (
            include_hiring_team
            if include_hiring_team is not None
            else self._settings.linkedin_enrich_include_hiring_team
        )

        params = {
            "job_url": job_url,
            "include_skills": "true" if skills else "false",
            "include_hiring_team": "true" if team else "false",
        }

        log = logger.bind(job_url=job_url)
        resp = await self._fetcher.get_json(
            f"{_BASE_URL}/get-job-details",
            params=params,
            headers=self._headers,
        )

        if not isinstance(resp.data, dict):
            log.warning("linkedin_job_details_unexpected_shape")
            return None

        data = resp.data.get("data")
        if not isinstance(data, dict):
            log.warning("linkedin_job_details_missing_data")
            return None

        log.debug("linkedin_job_details_fetched")
        return data
