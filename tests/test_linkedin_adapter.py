"""Tests for LinkedInAdapter — normalize(), jobs v1/v2 toggle, prospect fetchers."""

from __future__ import annotations

import httpx
import pytest
import respx

from api.schemas import ScrapeRequest
from config import Settings
from domain.models import SignalType
from infrastructure.fetchers.http import HttpFetcher
from modules.scraping.adapters.linkedin import (
    CompanySearchParams,
    LinkedInAdapter,
    _build_company_search_body,
    _to_target_company,
    _to_target_person,
)
from modules.scraping.signals import DEFAULT_CLASSIFIER


@pytest.fixture
def linkedin_settings() -> Settings:
    return Settings(
        linkedin_rapidapi_key="test-key",
        linkedin_rapidapi_host="test.rapidapi.com",
    )


@pytest.fixture
def adapter(linkedin_settings: Settings) -> LinkedInAdapter:
    client = httpx.AsyncClient()
    fetcher = HttpFetcher(client, user_agent="test/1.0")
    return LinkedInAdapter(fetcher=fetcher, settings=linkedin_settings)


class TestNormalizeJob:
    """Verify /search-jobs result normalization."""

    def test_basic_job(self, adapter: LinkedInAdapter) -> None:
        raw = {
            "_type": "job",
            "job_title": "Senior Python Developer",
            "company": "TechCorp",
            "location": "Remote",
            "job_url": "https://linkedin.com/jobs/view/12345",
            "job_id": "12345",
            "description": "We need a Python FastAPI expert.",
            "posted_date": "2026-04-10T12:00:00Z",
        }
        lead = adapter.normalize(raw, DEFAULT_CLASSIFIER)
        assert lead is not None
        assert lead.signal_type == SignalType.HIRING
        assert lead.signal_strength == 80
        assert lead.source == "linkedin"
        assert lead.company_name == "TechCorp"
        assert "TechCorp" in lead.title

    def test_stack_extraction(self, adapter: LinkedInAdapter) -> None:
        raw = {
            "_type": "job",
            "job_title": "Backend Engineer",
            "company": "StartupX",
            "job_url": "https://linkedin.com/jobs/1",
            "job_id": "1",
            "description": "Python, FastAPI, PostgreSQL, Docker, Kubernetes.",
        }
        lead = adapter.normalize(raw, DEFAULT_CLASSIFIER)
        assert lead is not None
        assert "python" in lead.keywords
        assert "fastapi" in lead.keywords
        assert "docker" in lead.keywords

    def test_empty_title_returns_none(self, adapter: LinkedInAdapter) -> None:
        raw = {"_type": "job", "job_title": "", "company": ""}
        assert adapter.normalize(raw, DEFAULT_CLASSIFIER) is None

    def test_location_preserved(self, adapter: LinkedInAdapter) -> None:
        raw = {
            "_type": "job",
            "job_title": "Developer",
            "company": "Co",
            "location": "San Francisco, CA",
            "job_url": "https://linkedin.com/jobs/2",
            "job_id": "2",
        }
        lead = adapter.normalize(raw, DEFAULT_CLASSIFIER)
        assert lead is not None
        assert lead.location == "San Francisco, CA"


class TestNormalizePost:
    """Verify /search-posts result normalization."""

    def test_hiring_post(self, adapter: LinkedInAdapter) -> None:
        raw = {
            "_type": "post",
            "text": "We're hiring a senior Python developer for our startup.",
            "poster_name": "Jane Founder",
            "poster_title": "CEO at StartupX",
            "post_url": "https://linkedin.com/feed/update/urn:li:activity:1",
            "post_id": "post_001",
            "posted": "2026-04-21 06:56:57.000",
        }
        lead = adapter.normalize(raw, DEFAULT_CLASSIFIER)
        assert lead is not None
        assert lead.signal_type == SignalType.HIRING
        assert lead.source == "linkedin"
        assert lead.person_name == "Jane Founder"
        assert lead.person_role == "CEO at StartupX"

    def test_linkedin_datetime_format_parsed(
        self, adapter: LinkedInAdapter
    ) -> None:
        """LinkedIn's 'YYYY-MM-DD HH:MM:SS.fff' format should parse."""
        raw = {
            "_type": "post",
            "text": "Looking for a senior engineer to join our team.",
            "poster_name": "Founder",
            "post_url": "https://linkedin.com/posts/founder",
            "post_id": "post_002",
            "posted": "2026-04-21 06:56:57.000",
        }
        lead = adapter.normalize(raw, DEFAULT_CLASSIFIER)
        assert lead is not None
        assert lead.posted_at is not None

    def test_tool_evaluation_post(self, adapter: LinkedInAdapter) -> None:
        raw = {
            "_type": "post",
            "text": "Evaluating alternatives to our current CI/CD setup.",
            "poster_name": "Dev Lead",
            "post_url": "https://linkedin.com/posts/devlead",
            "post_id": "post_003",
        }
        lead = adapter.normalize(raw, DEFAULT_CLASSIFIER)
        assert lead is not None
        assert lead.signal_type == SignalType.TOOL_EVALUATION

    def test_no_signal_returns_none(self, adapter: LinkedInAdapter) -> None:
        raw = {
            "_type": "post",
            "text": "Great weather today!",
            "poster_name": "Random Person",
            "post_url": "https://linkedin.com/posts/random",
            "post_id": "post_004",
        }
        assert adapter.normalize(raw, DEFAULT_CLASSIFIER) is None

    def test_empty_text_returns_none(self, adapter: LinkedInAdapter) -> None:
        raw = {"_type": "post", "text": "", "poster_name": "Nobody"}
        assert adapter.normalize(raw, DEFAULT_CLASSIFIER) is None

    def test_post_title_truncated(self, adapter: LinkedInAdapter) -> None:
        long_text = "We're hiring " + "x" * 200
        raw = {
            "_type": "post",
            "text": long_text,
            "post_url": "https://linkedin.com/posts/long",
            "post_id": "post_005",
        }
        lead = adapter.normalize(raw, DEFAULT_CLASSIFIER)
        assert lead is not None
        assert len(lead.title) <= 120


class TestFetchJobsV2:
    """Verify the jobs v1/v2 toggle honors LEADS_LINKEDIN_USE_JOBS_V2."""

    @respx.mock
    @pytest.mark.asyncio
    async def test_v2_selected_by_default(self) -> None:
        """When the toggle is true, /search-jobs-v2 is called with {keywords, location, start}."""
        client = httpx.AsyncClient()
        fetcher = HttpFetcher(client, user_agent="test/1.0")
        settings = Settings(
            linkedin_rapidapi_key="k",
            linkedin_job_queries=["python"],
            linkedin_post_queries=[],
            linkedin_use_jobs_v2=True,
            linkedin_job_location="Remote",
            linkedin_job_start=0,
        )
        adapter = LinkedInAdapter(fetcher=fetcher, settings=settings)

        v2_route = respx.post(
            "https://fresh-linkedin-profile-data.p.rapidapi.com/search-jobs-v2"
        ).respond(200, json={"data": [{"job_id": "1", "job_title": "Python Dev"}]})

        raw = await adapter.fetch_raw(ScrapeRequest())
        assert v2_route.called
        body = v2_route.calls[0].request.read()
        assert b'"location":"Remote"' in body
        assert b'"start":0' in body
        assert b'"keywords":"python"' in body
        assert len(raw) == 1 and raw[0]["_type"] == "job"

    @respx.mock
    @pytest.mark.asyncio
    async def test_v1_used_when_toggle_off(self) -> None:
        """When the toggle is false, legacy /search-jobs is called with {keywords, page}."""
        client = httpx.AsyncClient()
        fetcher = HttpFetcher(client, user_agent="test/1.0")
        settings = Settings(
            linkedin_rapidapi_key="k",
            linkedin_job_queries=["python"],
            linkedin_post_queries=[],
            linkedin_use_jobs_v2=False,
        )
        adapter = LinkedInAdapter(fetcher=fetcher, settings=settings)

        v1_route = respx.post(
            "https://fresh-linkedin-profile-data.p.rapidapi.com/search-jobs"
        ).respond(200, json={"data": [{"job_id": "1", "job_title": "Python Dev"}]})

        await adapter.fetch_raw(ScrapeRequest())
        assert v1_route.called
        body = v1_route.calls[0].request.read()
        assert b'"page":1' in body


class TestBuildCompanySearchBody:
    """Pure-function checks on the /search-companies body builder."""

    def test_minimal_body(self) -> None:
        params = CompanySearchParams(limit=10)
        body = _build_company_search_body(params)
        assert body["limit"] == 10
        assert body["hiring_on_linkedin"] == "false"
        # Ranges are only included when both bounds are set.
        assert "annual_revenue" not in body
        assert "company_headcount_growth" not in body

    def test_full_body(self) -> None:
        params = CompanySearchParams(
            headcounts=["11-50"],
            industry_codes=[3, 4],
            hq_location_codes=[103644278],
            technologies=["jQuery"],
            keywords="startup",
            headcount_growth_min=-5,
            headcount_growth_max=10,
            annual_revenue_min=1,
            annual_revenue_max=10,
            annual_revenue_currency="CAD",
            hiring_on_linkedin=True,
            limit=25,
        )
        body = _build_company_search_body(params)
        assert body["annual_revenue"] == {"min": 1, "max": 10, "currency": "CAD"}
        assert body["company_headcount_growth"] == {"min": -5, "max": 10}
        assert body["hiring_on_linkedin"] == "true"
        assert body["technologies_used"] == ["jQuery"]


class TestFetchTargetCompanies:
    """Verify /search-companies response is normalized into TargetCompany objects."""

    @respx.mock
    @pytest.mark.asyncio
    async def test_parses_response(self) -> None:
        client = httpx.AsyncClient()
        fetcher = HttpFetcher(client, user_agent="test/1.0")
        settings = Settings(linkedin_rapidapi_key="k")
        adapter = LinkedInAdapter(fetcher=fetcher, settings=settings)

        respx.post(
            "https://fresh-linkedin-profile-data.p.rapidapi.com/search-companies"
        ).respond(
            200,
            json={
                "data": [
                    {
                        "company_id": "c1",
                        "name": "Acme Inc",
                        "linkedin_url": "https://linkedin.com/company/acme",
                        "website": "acme.io",
                        "industry": "SaaS",
                        "headcount": "51-200",
                        "headcount_growth": 12,
                        "annual_revenue": {"min": 1, "max": 10, "currency": "USD"},
                        "headquarters": "SF",
                        "technologies_used": ["python", "postgres"],
                        "hiring_on_linkedin": True,
                    }
                ]
            },
        )

        results = await adapter.fetch_target_companies(CompanySearchParams(limit=1))
        assert len(results) == 1
        c = results[0]
        assert c.name == "Acme Inc"
        assert c.source == "linkedin"
        assert c.source_id == "c1"
        assert c.domain == "acme.io"
        assert c.headcount_band == "51-200"
        assert c.headcount_growth == 12
        assert c.annual_revenue_currency == "USD"
        assert c.technologies == ["python", "postgres"]

    def test_to_target_company_rejects_empty_name(self) -> None:
        with pytest.raises(ValueError):
            _to_target_company({"company_id": "c1"})


class TestFetchTargetPeople:
    """Verify /search-employees responses become TargetPerson objects."""

    @respx.mock
    @pytest.mark.asyncio
    async def test_parses_response_with_seed_url(self) -> None:
        client = httpx.AsyncClient()
        fetcher = HttpFetcher(client, user_agent="test/1.0")
        settings = Settings(linkedin_rapidapi_key="k")
        adapter = LinkedInAdapter(fetcher=fetcher, settings=settings)

        seed_url = "https://www.linkedin.com/sales/search/people?query=foo"
        respx.post(
            "https://fresh-linkedin-profile-data.p.rapidapi.com/search-employees"
        ).respond(
            200,
            json={
                "data": [
                    {
                        "profile_id": "p1",
                        "full_name": "Jane Doe",
                        "headline": "CTO at Acme",
                        "current_title": "CTO",
                        "current_company": "Acme Inc",
                        "current_company_domain": "acme.io",
                        "profile_url": "https://linkedin.com/in/jane",
                        "location": "SF",
                    }
                ]
            },
        )

        results = await adapter.fetch_target_people(seed_urls=[seed_url], limit=10)
        assert len(results) == 1
        p = results[0]
        assert p.full_name == "Jane Doe"
        assert p.source_id == "p1"
        assert p.current_company_domain == "acme.io"
        assert p.seed_url == seed_url

    def test_to_target_person_builds_name_from_parts(self) -> None:
        person = _to_target_person(
            {
                "profile_id": "p2",
                "first_name": "John",
                "last_name": "Smith",
            },
            seed_url="https://sales.linkedin.com/x",
        )
        assert person.full_name == "John Smith"

    def test_to_target_person_rejects_no_name(self) -> None:
        with pytest.raises(ValueError):
            _to_target_person({"profile_id": "p3"}, seed_url="x")
