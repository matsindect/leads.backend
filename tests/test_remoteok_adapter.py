"""Tests for RemoteOKAdapter.normalize() using fixture RSS data."""

from __future__ import annotations

from pathlib import Path

import httpx
import pytest
import respx

from config import Settings
from domain.models import SignalType
from infrastructure.fetchers.http import HttpFetcher
from infrastructure.fetchers.rss import RssFetcher
from modules.scraping.adapters.remoteok import RemoteOKAdapter

FIXTURES = Path(__file__).parent / "fixtures"


@pytest.fixture
def rss_fetcher() -> RssFetcher:
    client = httpx.AsyncClient()
    http = HttpFetcher(client, user_agent="test/1.0")
    return RssFetcher(http)


@pytest.fixture
def adapter(rss_fetcher: RssFetcher, settings: Settings) -> RemoteOKAdapter:
    return RemoteOKAdapter(fetcher=rss_fetcher, settings=settings)


class TestRemoteOKNormalize:
    """Verify normalize() filtering and field extraction."""

    def test_dev_role_accepted(self, adapter: RemoteOKAdapter) -> None:
        raw = {
            "id": "https://remoteok.com/remote-jobs/12345",
            "title": "Acme Corp - Senior Python Developer",
            "link": "https://remoteok.com/remote-jobs/12345",
            "summary": "Looking for a senior Python dev.",
            "published_at": None,
            "author": "jobs@acmecorp.io",
        }
        lead = adapter.normalize(raw)
        assert lead is not None
        assert lead.signal_type == SignalType.HIRING
        assert lead.signal_strength == 70
        assert lead.source == "remoteok"

    def test_non_dev_role_filtered(self, adapter: RemoteOKAdapter) -> None:
        raw = {
            "id": "https://remoteok.com/remote-jobs/12347",
            "title": "DesignStudio - UI/UX Designer",
            "link": "https://remoteok.com/remote-jobs/12347",
            "summary": "Figma expert needed.",
            "published_at": None,
            "author": None,
        }
        assert adapter.normalize(raw) is None

    def test_company_name_extracted_from_title(self, adapter: RemoteOKAdapter) -> None:
        raw = {
            "id": "1",
            "title": "StartupX - Frontend React Engineer",
            "link": "https://remoteok.com/1",
            "summary": "",
            "published_at": None,
            "author": None,
        }
        lead = adapter.normalize(raw)
        assert lead is not None
        assert lead.company_name == "StartupX"

    def test_domain_from_author_email(self, adapter: RemoteOKAdapter) -> None:
        raw = {
            "id": "2",
            "title": "Some Company - Backend Developer",
            "link": "https://remoteok.com/2",
            "summary": "",
            "published_at": None,
            "author": "hr@somecompany.com",
        }
        lead = adapter.normalize(raw)
        assert lead is not None
        assert lead.company_domain == "somecompany.com"

    def test_no_author_email(self, adapter: RemoteOKAdapter) -> None:
        raw = {
            "id": "3",
            "title": "Anon Inc - Software Engineer",
            "link": "https://remoteok.com/3",
            "summary": "",
            "published_at": None,
            "author": None,
        }
        lead = adapter.normalize(raw)
        assert lead is not None
        assert lead.company_domain is None


class TestRemoteOKFetchRaw:
    """Verify fetch_raw() integration with RssFetcher."""

    @respx.mock
    @pytest.mark.asyncio
    async def test_fetches_and_returns_entries(self, adapter: RemoteOKAdapter) -> None:
        xml = (FIXTURES / "sample_rss.xml").read_text()
        respx.get("https://remoteok.com/remote-jobs.rss").respond(200, text=xml)

        entries = await adapter.fetch_raw()
        assert len(entries) == 3
        assert entries[0]["title"] == "Acme Corp - Senior Python Developer"
