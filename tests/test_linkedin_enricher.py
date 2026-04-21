"""Tests for LinkedInJobEnricher — /get-job-details wrapper."""

from __future__ import annotations

import httpx
import pytest
import respx

from config import Settings
from infrastructure.fetchers.base import PermanentFetcherError
from infrastructure.fetchers.http import HttpFetcher
from modules.enrichment.linkedin_enricher import LinkedInJobEnricher


@pytest.fixture
def enricher_settings() -> Settings:
    return Settings(
        linkedin_rapidapi_key="k",
        linkedin_rapidapi_host="fresh-linkedin-profile-data.p.rapidapi.com",
        linkedin_enrich_include_skills=False,
        linkedin_enrich_include_hiring_team=False,
    )


@pytest.fixture
def enricher(enricher_settings: Settings) -> LinkedInJobEnricher:
    client = httpx.AsyncClient()
    fetcher = HttpFetcher(client, user_agent="test/1.0")
    return LinkedInJobEnricher(fetcher=fetcher, settings=enricher_settings)


class TestFetchJobDetails:

    @respx.mock
    @pytest.mark.asyncio
    async def test_returns_data_block(
        self, enricher: LinkedInJobEnricher
    ) -> None:
        respx.get(
            "https://fresh-linkedin-profile-data.p.rapidapi.com/get-job-details"
        ).respond(
            200,
            json={"data": {"job_title": "Senior Python Dev", "company": "Acme"}},
        )

        result = await enricher.fetch_job_details(
            "https://www.linkedin.com/jobs/view/1234"
        )
        assert result == {"job_title": "Senior Python Dev", "company": "Acme"}

    @respx.mock
    @pytest.mark.asyncio
    async def test_missing_data_returns_none(
        self, enricher: LinkedInJobEnricher
    ) -> None:
        respx.get(
            "https://fresh-linkedin-profile-data.p.rapidapi.com/get-job-details"
        ).respond(200, json={"status": "ok"})
        result = await enricher.fetch_job_details("https://x")
        assert result is None

    @respx.mock
    @pytest.mark.asyncio
    async def test_non_dict_response_returns_none(
        self, enricher: LinkedInJobEnricher
    ) -> None:
        respx.get(
            "https://fresh-linkedin-profile-data.p.rapidapi.com/get-job-details"
        ).respond(200, json=["not", "a", "dict"])
        result = await enricher.fetch_job_details("https://x")
        assert result is None

    @respx.mock
    @pytest.mark.asyncio
    async def test_per_call_flags_override_settings(
        self, enricher: LinkedInJobEnricher
    ) -> None:
        route = respx.get(
            "https://fresh-linkedin-profile-data.p.rapidapi.com/get-job-details"
        ).respond(200, json={"data": {}})
        await enricher.fetch_job_details(
            "https://x", include_skills=True, include_hiring_team=True
        )
        req = route.calls[0].request
        assert "include_skills=true" in str(req.url)
        assert "include_hiring_team=true" in str(req.url)

    @respx.mock
    @pytest.mark.asyncio
    async def test_404_propagates(
        self, enricher: LinkedInJobEnricher
    ) -> None:
        respx.get(
            "https://fresh-linkedin-profile-data.p.rapidapi.com/get-job-details"
        ).respond(404, json={"error": "not found"})
        with pytest.raises(PermanentFetcherError):
            await enricher.fetch_job_details("https://x")
