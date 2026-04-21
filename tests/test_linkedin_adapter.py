"""Tests for LinkedInAdapter.normalize() — RapidAPI-based, POST endpoints."""

from __future__ import annotations

import httpx
import pytest

from config import Settings
from domain.models import SignalType
from infrastructure.fetchers.http import HttpFetcher
from modules.scraping.adapters.linkedin import LinkedInAdapter


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
        lead = adapter.normalize(raw)
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
        lead = adapter.normalize(raw)
        assert lead is not None
        assert "python" in lead.stack_mentions
        assert "fastapi" in lead.stack_mentions
        assert "docker" in lead.stack_mentions

    def test_empty_title_returns_none(self, adapter: LinkedInAdapter) -> None:
        raw = {"_type": "job", "job_title": "", "company": ""}
        assert adapter.normalize(raw) is None

    def test_location_preserved(self, adapter: LinkedInAdapter) -> None:
        raw = {
            "_type": "job",
            "job_title": "Developer",
            "company": "Co",
            "location": "San Francisco, CA",
            "job_url": "https://linkedin.com/jobs/2",
            "job_id": "2",
        }
        lead = adapter.normalize(raw)
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
        lead = adapter.normalize(raw)
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
        lead = adapter.normalize(raw)
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
        lead = adapter.normalize(raw)
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
        assert adapter.normalize(raw) is None

    def test_empty_text_returns_none(self, adapter: LinkedInAdapter) -> None:
        raw = {"_type": "post", "text": "", "poster_name": "Nobody"}
        assert adapter.normalize(raw) is None

    def test_post_title_truncated(self, adapter: LinkedInAdapter) -> None:
        long_text = "We're hiring " + "x" * 200
        raw = {
            "_type": "post",
            "text": long_text,
            "post_url": "https://linkedin.com/posts/long",
            "post_id": "post_005",
        }
        lead = adapter.normalize(raw)
        assert lead is not None
        assert len(lead.title) <= 120
