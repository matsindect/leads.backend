"""Tests for RssMultiAdapter.normalize()."""

from __future__ import annotations

from pathlib import Path

import httpx
import pytest
import respx

from config import Settings
from domain.models import SignalType
from infrastructure.fetchers.http import HttpFetcher
from infrastructure.fetchers.rss import RssFetcher
from modules.scraping.adapters.rss_multi import RssMultiAdapter

FIXTURES = Path(__file__).parent / "fixtures"


@pytest.fixture
def adapter(settings: Settings) -> RssMultiAdapter:
    client = httpx.AsyncClient()
    http = HttpFetcher(client, user_agent="test/1.0")
    rss = RssFetcher(http)
    return RssMultiAdapter(fetcher=rss, settings=settings)


class TestRssMultiNormalize:

    def test_classified_entry(self, adapter: RssMultiAdapter) -> None:
        raw = {
            "id": "1", "title": "We're hiring Python developers",
            "link": "https://example.com/1",
            "summary": "Join our team at acme.io",
            "published_at": None, "author": "poster",
        }
        lead = adapter.normalize(raw)
        assert lead is not None
        assert lead.signal_type == SignalType.HIRING
        assert lead.source == "rss"

    def test_unclassified_defaults_to_general_interest(self, adapter: RssMultiAdapter) -> None:
        raw = {
            "id": "2", "title": "Weekly newsletter update",
            "link": "https://example.com/2",
            "summary": "This week in tech news.",
            "published_at": None, "author": None,
        }
        lead = adapter.normalize(raw)
        assert lead is not None
        assert lead.signal_type == SignalType.GENERAL_INTEREST
        assert lead.signal_strength == 20

    def test_stack_and_domain_extracted(self, adapter: RssMultiAdapter) -> None:
        raw = {
            "id": "3", "title": "Migrating from Jenkins to GitHub Actions",
            "link": "https://example.com/3",
            "summary": "Our team at devco.io switched to docker and kubernetes.",
            "published_at": None, "author": None,
        }
        lead = adapter.normalize(raw)
        assert lead is not None
        assert "docker" in lead.stack_mentions
        assert "kubernetes" in lead.stack_mentions
        assert lead.company_domain == "devco.io"


class TestRssMultiFetchRaw:

    @respx.mock
    @pytest.mark.asyncio
    async def test_merges_multiple_feeds(self) -> None:
        client = httpx.AsyncClient()
        http = HttpFetcher(client, user_agent="test/1.0")
        rss = RssFetcher(http)
        s = Settings(rss_feed_urls=["https://a.com/feed", "https://b.com/feed"])
        adapter = RssMultiAdapter(fetcher=rss, settings=s)

        xml = (FIXTURES / "sample_rss.xml").read_text()
        respx.get("https://a.com/feed").respond(200, text=xml)
        respx.get("https://b.com/feed").respond(200, text=xml)

        entries = await adapter.fetch_raw()
        # sample_rss.xml has 3 entries × 2 feeds
        assert len(entries) == 6
