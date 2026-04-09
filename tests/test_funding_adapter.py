"""Tests for FundingAdapter.normalize() and funding-specific logic."""

from __future__ import annotations

from pathlib import Path

import httpx
import pytest
import respx

from config import Settings
from domain.models import SignalType
from infrastructure.fetchers.http import HttpFetcher
from infrastructure.fetchers.rss import RssFetcher
from modules.scraping.adapters.funding import FundingAdapter

FIXTURES = Path(__file__).parent / "fixtures"


@pytest.fixture
def adapter(settings: Settings) -> FundingAdapter:
    client = httpx.AsyncClient()
    http = HttpFetcher(client, user_agent="test/1.0")
    rss = RssFetcher(http)
    return FundingAdapter(fetcher=rss, settings=settings)


class TestFundingNormalize:

    def test_series_a_high_strength(self, adapter: FundingAdapter) -> None:
        raw = {
            "id": "1", "title": "Acme AI raises $25M Series A to build developer tools",
            "link": "https://tc.com/1", "summary": "Startup at acme.ai raised funds.",
            "published_at": None, "author": None,
        }
        lead = adapter.normalize(raw)
        assert lead is not None
        assert lead.signal_type == SignalType.FUNDING
        assert lead.signal_strength == 90
        assert lead.company_name == "Acme AI"

    def test_seed_round_medium_strength(self, adapter: FundingAdapter) -> None:
        raw = {
            "id": "2", "title": "DevStack secures $3M seed round for its Kubernetes platform",
            "link": "https://tc.com/2", "summary": "DevStack from devstack.io builds K8s tooling.",
            "published_at": None, "author": None,
        }
        lead = adapter.normalize(raw)
        assert lead is not None
        assert lead.signal_strength == 80
        assert lead.company_name == "DevStack"

    def test_generic_funding_default_strength(self, adapter: FundingAdapter) -> None:
        raw = {
            "id": "3", "title": "Glamify gets acquired by BigRetail",
            "link": "https://tc.com/3", "summary": "Fashion startup acquired.",
            "published_at": None, "author": None,
        }
        lead = adapter.normalize(raw)
        assert lead is not None
        assert lead.signal_strength == 70

    def test_domain_extracted(self, adapter: FundingAdapter) -> None:
        raw = {
            "id": "4", "title": "NewCo raises $10M",
            "link": "https://tc.com/4", "summary": "The team at newco.io is expanding.",
            "published_at": None, "author": None,
        }
        lead = adapter.normalize(raw)
        assert lead is not None
        assert lead.company_domain == "newco.io"

    def test_stack_mentions_extracted(self, adapter: FundingAdapter) -> None:
        raw = {
            "id": "5", "title": "DevStack secures seed",
            "link": "https://tc.com/5",
            "summary": "They use Go and Terraform and kubernetes.",
            "published_at": None, "author": None,
        }
        lead = adapter.normalize(raw)
        assert lead is not None
        assert "kubernetes" in lead.stack_mentions
        assert "terraform" in lead.stack_mentions


class TestFundingFetchRaw:

    @respx.mock
    @pytest.mark.asyncio
    async def test_fetches_entries(self, settings: Settings) -> None:
        client = httpx.AsyncClient()
        http = HttpFetcher(client, user_agent="test/1.0")
        rss = RssFetcher(http)
        s = Settings(funding_feed_urls=["https://tc.com/feed"])
        adapter = FundingAdapter(fetcher=rss, settings=s)

        xml = (FIXTURES / "sample_funding_rss.xml").read_text()
        respx.get("https://tc.com/feed").respond(200, text=xml)

        entries = await adapter.fetch_raw()
        assert len(entries) == 3
