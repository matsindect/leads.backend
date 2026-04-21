"""Tests for GoogleCSEAdapter."""

from __future__ import annotations

import json
from pathlib import Path

import httpx
import pytest
import respx

from api.schemas import ScrapeRequest
from config import Settings
from domain.models import SignalType
from infrastructure.fetchers.http import HttpFetcher
from modules.scraping.adapters.google_cse import GoogleCSEAdapter, _DailyBudget
from modules.scraping.signals import DEFAULT_CLASSIFIER

FIXTURES = Path(__file__).parent / "fixtures"


@pytest.fixture
def cse_settings() -> Settings:
    return Settings(
        google_cse_api_key="test-key",
        google_cse_engine_id="test-cx",
        google_cse_queries=["hiring developer"],
        google_cse_daily_query_budget=100,
    )


@pytest.fixture
def adapter(cse_settings: Settings) -> GoogleCSEAdapter:
    client = httpx.AsyncClient()
    fetcher = HttpFetcher(client, user_agent="test/1.0")
    return GoogleCSEAdapter(fetcher=fetcher, settings=cse_settings)


class TestGoogleCSENormalize:

    def test_hiring_signal(self, adapter: GoogleCSEAdapter) -> None:
        raw = {
            "title": "We're hiring senior Python engineers - r/startups",
            "link": "https://reddit.com/r/startups/abc123",
            "snippet": "Our startup at acme.io is looking for Python developers.",
        }
        lead = adapter.normalize(raw, DEFAULT_CLASSIFIER)
        assert lead is not None
        assert lead.signal_type == SignalType.HIRING
        assert lead.source == "google_cse"

    def test_tool_evaluation_signal(self, adapter: GoogleCSEAdapter) -> None:
        raw = {
            "title": "Evaluating alternatives to Datadog",
            "link": "https://news.ycombinator.com/item?id=99999",
            "snippet": "Comparing monitoring tools with kubernetes and AWS.",
        }
        lead = adapter.normalize(raw, DEFAULT_CLASSIFIER)
        assert lead is not None
        assert lead.signal_type == SignalType.TOOL_EVALUATION

    def test_no_signal_returns_none(self, adapter: GoogleCSEAdapter) -> None:
        raw = {
            "title": "Best sunset photos of 2024",
            "link": "https://example.com/sunsets",
            "snippet": "Amazing photographs from around the world.",
        }
        assert adapter.normalize(raw, DEFAULT_CLASSIFIER) is None

    def test_domain_from_result_url(self, adapter: GoogleCSEAdapter) -> None:
        """Non-platform URLs use the result domain as company_domain."""
        raw = {
            "title": "We're hiring at acme.io",
            "link": "https://acme.io/careers",
            "snippet": "Join our team.",
        }
        lead = adapter.normalize(raw, DEFAULT_CLASSIFIER)
        assert lead is not None
        assert lead.company_domain == "acme.io"

    def test_platform_url_skipped_for_domain(self, adapter: GoogleCSEAdapter) -> None:
        """Reddit/HN URLs should not become company_domain."""
        raw = {
            "title": "Hiring developers at startup.io",
            "link": "https://www.reddit.com/r/startups/abc",
            "snippet": "Looking for devs at startup.io",
        }
        lead = adapter.normalize(raw, DEFAULT_CLASSIFIER)
        assert lead is not None
        assert lead.company_domain == "startup.io"

    def test_keywords(self, adapter: GoogleCSEAdapter) -> None:
        raw = {
            "title": "Hiring Python + FastAPI developer",
            "link": "https://example.com/job",
            "snippet": "We use docker and kubernetes.",
        }
        lead = adapter.normalize(raw, DEFAULT_CLASSIFIER)
        assert lead is not None
        assert "python" in lead.keywords
        assert "docker" in lead.keywords


class TestGoogleCSEFetchRaw:

    @respx.mock
    @pytest.mark.asyncio
    async def test_fetches_results(self, cse_settings: Settings) -> None:
        client = httpx.AsyncClient()
        fetcher = HttpFetcher(client, user_agent="test/1.0")
        adapter = GoogleCSEAdapter(fetcher=fetcher, settings=cse_settings)

        data = json.loads((FIXTURES / "sample_google_cse_response.json").read_text())
        respx.get("https://www.googleapis.com/customsearch/v1").respond(200, json=data)

        results = await adapter.fetch_raw(ScrapeRequest())
        assert len(results) == 3

    @respx.mock
    @pytest.mark.asyncio
    async def test_budget_exhausted_stops_queries(self) -> None:
        client = httpx.AsyncClient()
        fetcher = HttpFetcher(client, user_agent="test/1.0")
        s = Settings(
            google_cse_api_key="k", google_cse_engine_id="cx",
            google_cse_queries=["q1", "q2", "q3"],
            google_cse_daily_query_budget=1,
        )
        adapter = GoogleCSEAdapter(fetcher=fetcher, settings=s)

        route = respx.get("https://www.googleapis.com/customsearch/v1").respond(
            200, json={"items": [{"title": "t", "link": "l", "snippet": "s"}]}
        )

        await adapter.fetch_raw(ScrapeRequest())
        # Budget=1, so only 1 query should have been made
        assert route.call_count == 1


class TestDailyBudget:

    def test_basic_counting(self) -> None:
        b = _DailyBudget(max_queries=2)
        assert b.can_query()
        b.record_query()
        assert b.can_query()
        b.record_query()
        assert not b.can_query()
        assert b.remaining == 0
