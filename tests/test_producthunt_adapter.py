"""Tests for ProductHuntAdapter.normalize()."""

from __future__ import annotations

from pathlib import Path

import httpx
import pytest
import respx

from api.schemas import ScrapeRequest
from config import Settings
from domain.models import SignalType
from infrastructure.fetchers.http import HttpFetcher
from infrastructure.fetchers.rss import RssFetcher
from modules.scraping.adapters.producthunt import ProductHuntAdapter
from modules.scraping.signals import DEFAULT_CLASSIFIER

FIXTURES = Path(__file__).parent / "fixtures"


@pytest.fixture
def adapter(settings: Settings) -> ProductHuntAdapter:
    client = httpx.AsyncClient()
    http = HttpFetcher(client, user_agent="test/1.0")
    rss = RssFetcher(http)
    return ProductHuntAdapter(fetcher=rss, settings=settings)


class TestProductHuntNormalize:

    def test_tool_evaluation_signal(self, adapter: ProductHuntAdapter) -> None:
        """Entry mentioning 'alternative to' should classify as TOOL_EVALUATION."""
        raw = {
            "id": "1", "title": "CodeReview AI — AI-powered code review for your team",
            "link": "https://producthunt.com/posts/codereview-ai",
            "summary": "An alternative to existing review tools. Built with Python.",
            "published_at": None, "author": None,
        }
        lead = adapter.normalize(raw, DEFAULT_CLASSIFIER)
        assert lead is not None
        assert lead.signal_type == SignalType.TOOL_EVALUATION
        assert lead.source == "producthunt"

    def test_expansion_signal(self, adapter: ProductHuntAdapter) -> None:
        raw = {
            "id": "3", "title": "ScaleDB — Database scaling made simple",
            "link": "https://producthunt.com/posts/scaledb",
            "summary": "Scale your postgres database. Growing fast.",
            "published_at": None, "author": None,
        }
        lead = adapter.normalize(raw, DEFAULT_CLASSIFIER)
        assert lead is not None
        assert lead.signal_type == SignalType.EXPANSION

    def test_default_general_interest(self, adapter: ProductHuntAdapter) -> None:
        """Entry with no strong signal defaults to GENERAL_INTEREST."""
        raw = {
            "id": "2", "title": "DesignFlow — Figma plugin for design systems",
            "link": "https://producthunt.com/posts/designflow",
            "summary": "Create and manage design systems in Figma.",
            "published_at": None, "author": None,
        }
        lead = adapter.normalize(raw, DEFAULT_CLASSIFIER)
        assert lead is not None
        assert lead.signal_type == SignalType.GENERAL_INTEREST
        assert lead.signal_strength == 40

    def test_product_name_extracted(self, adapter: ProductHuntAdapter) -> None:
        raw = {
            "id": "1", "title": "CodeReview AI — AI-powered code review",
            "link": "https://producthunt.com/posts/codereview-ai",
            "summary": "", "published_at": None, "author": None,
        }
        lead = adapter.normalize(raw, DEFAULT_CLASSIFIER)
        assert lead is not None
        assert lead.company_name == "CodeReview AI"

    def test_stack_extraction(self, adapter: ProductHuntAdapter) -> None:
        raw = {
            "id": "1", "title": "Tool — stuff",
            "link": "https://producthunt.com/posts/tool",
            "summary": "Built with python and fastapi on kubernetes.",
            "published_at": None, "author": None,
        }
        lead = adapter.normalize(raw, DEFAULT_CLASSIFIER)
        assert lead is not None
        assert "python" in lead.keywords
        assert "fastapi" in lead.keywords


class TestProductHuntFetchRaw:

    @respx.mock
    @pytest.mark.asyncio
    async def test_fetches_entries(self, settings: Settings) -> None:
        client = httpx.AsyncClient()
        http = HttpFetcher(client, user_agent="test/1.0")
        rss = RssFetcher(http)
        adapter = ProductHuntAdapter(fetcher=rss, settings=settings)

        xml = (FIXTURES / "sample_producthunt_rss.xml").read_text()
        respx.get("https://www.producthunt.com/feed").respond(200, text=xml)

        entries = await adapter.fetch_raw(ScrapeRequest())
        assert len(entries) == 3
