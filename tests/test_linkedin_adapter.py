"""Tests for LinkedInAdapter.normalize() — API-based, no Playwright."""

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
    """Verify job search result normalization."""

    def test_basic_job(self, adapter: LinkedInAdapter) -> None:
        raw = {
            "_type": "job",
            "job_title": "Senior Python Developer",
            "company_name": "TechCorp",
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
        assert "Senior Python Developer" in lead.title

    def test_stack_extraction(self, adapter: LinkedInAdapter) -> None:
        raw = {
            "_type": "job",
            "job_title": "Backend Engineer",
            "company_name": "StartupX",
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
        raw = {"_type": "job", "job_title": "", "company_name": ""}
        assert adapter.normalize(raw) is None

    def test_location_preserved(self, adapter: LinkedInAdapter) -> None:
        raw = {
            "_type": "job",
            "job_title": "Developer",
            "company_name": "Co",
            "location": "San Francisco, CA",
            "job_url": "https://linkedin.com/jobs/2",
            "job_id": "2",
        }
        lead = adapter.normalize(raw)
        assert lead is not None
        assert lead.location == "San Francisco, CA"

    def test_posted_date_parsed(self, adapter: LinkedInAdapter) -> None:
        raw = {
            "_type": "job",
            "job_title": "Dev",
            "company_name": "Co",
            "job_url": "https://linkedin.com/jobs/3",
            "job_id": "3",
            "posted_date": "2026-04-10T15:30:00Z",
        }
        lead = adapter.normalize(raw)
        assert lead is not None
        assert lead.posted_at is not None
        assert lead.posted_at.tzinfo is not None


class TestNormalizePost:
    """Verify post search result normalization."""

    def test_hiring_post(self, adapter: LinkedInAdapter) -> None:
        raw = {
            "_type": "post",
            "text": "We're hiring a senior Python developer for our startup.",
            "author_name": "Jane Founder",
            "post_url": "https://linkedin.com/posts/jane_hiring",
            "post_id": "post_001",
            "posted_date": "2026-04-10T12:00:00Z",
        }
        lead = adapter.normalize(raw)
        assert lead is not None
        assert lead.signal_type == SignalType.HIRING
        assert lead.source == "linkedin"
        assert lead.person_name == "Jane Founder"

    def test_tool_evaluation_post(self, adapter: LinkedInAdapter) -> None:
        raw = {
            "_type": "post",
            "text": "Evaluating alternatives to our current CI/CD setup.",
            "author_name": "Dev Lead",
            "post_url": "https://linkedin.com/posts/devlead_eval",
            "post_id": "post_002",
        }
        lead = adapter.normalize(raw)
        assert lead is not None
        assert lead.signal_type == SignalType.TOOL_EVALUATION

    def test_no_signal_returns_none(self, adapter: LinkedInAdapter) -> None:
        raw = {
            "_type": "post",
            "text": "Great weather today!",
            "author_name": "Random Person",
            "post_url": "https://linkedin.com/posts/random",
            "post_id": "post_003",
        }
        assert adapter.normalize(raw) is None

    def test_empty_text_returns_none(self, adapter: LinkedInAdapter) -> None:
        raw = {"_type": "post", "text": "", "author_name": "Nobody"}
        assert adapter.normalize(raw) is None

    def test_post_title_truncated(self, adapter: LinkedInAdapter) -> None:
        long_text = "We're hiring " + "x" * 200
        raw = {
            "_type": "post",
            "text": long_text,
            "post_url": "https://linkedin.com/posts/long",
            "post_id": "post_004",
        }
        lead = adapter.normalize(raw)
        assert lead is not None
        assert len(lead.title) <= 120
