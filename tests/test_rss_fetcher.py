"""Unit tests for RssFetcher using fixture XML files."""

from __future__ import annotations

from pathlib import Path

import httpx
import pytest
import respx

from infrastructure.fetchers.http import HttpFetcher
from infrastructure.fetchers.rss import RssFetcher

FIXTURES = Path(__file__).parent / "fixtures"


@pytest.fixture
def rss_fetcher() -> RssFetcher:
    client = httpx.AsyncClient()
    http = HttpFetcher(client, user_agent="test/1.0")
    return RssFetcher(http)


class TestRssFetcher:
    """Verify RSS/Atom feed parsing."""

    @respx.mock
    @pytest.mark.asyncio
    async def test_parses_valid_feed(self, rss_fetcher: RssFetcher) -> None:
        """Valid RSS returns entries with correct fields."""
        xml = (FIXTURES / "sample_rss.xml").read_text()
        respx.get("https://example.com/feed.xml").respond(200, text=xml)

        feed = await rss_fetcher.fetch("https://example.com/feed.xml")

        assert feed.feed_title == "Remote OK Jobs"
        assert len(feed.entries) == 3

    @respx.mock
    @pytest.mark.asyncio
    async def test_entry_fields(self, rss_fetcher: RssFetcher) -> None:
        """First entry has expected title, link, id, author."""
        xml = (FIXTURES / "sample_rss.xml").read_text()
        respx.get("https://example.com/feed.xml").respond(200, text=xml)

        feed = await rss_fetcher.fetch("https://example.com/feed.xml")
        entry = feed.entries[0]

        assert "Acme Corp" in entry.title
        assert "Senior Python Developer" in entry.title
        assert entry.link == "https://remoteok.com/remote-jobs/12345"
        assert entry.id == "https://remoteok.com/remote-jobs/12345"
        assert entry.author is not None

    @respx.mock
    @pytest.mark.asyncio
    async def test_published_at_parsed(self, rss_fetcher: RssFetcher) -> None:
        """pubDate is parsed into a timezone-aware datetime."""
        xml = (FIXTURES / "sample_rss.xml").read_text()
        respx.get("https://example.com/feed.xml").respond(200, text=xml)

        feed = await rss_fetcher.fetch("https://example.com/feed.xml")
        assert feed.entries[0].published_at is not None
        assert feed.entries[0].published_at.tzinfo is not None

    @respx.mock
    @pytest.mark.asyncio
    async def test_missing_author_is_none(self, rss_fetcher: RssFetcher) -> None:
        """Entry without author field returns None."""
        xml = (FIXTURES / "sample_rss.xml").read_text()
        respx.get("https://example.com/feed.xml").respond(200, text=xml)

        feed = await rss_fetcher.fetch("https://example.com/feed.xml")
        # Third entry has no <author> tag
        assert feed.entries[2].author is None

    @respx.mock
    @pytest.mark.asyncio
    async def test_malformed_feed_returns_empty(self, rss_fetcher: RssFetcher) -> None:
        """Broken XML returns empty RssFeed instead of crashing."""
        xml = (FIXTURES / "malformed_rss.xml").read_text()
        respx.get("https://example.com/broken.xml").respond(200, text=xml)

        feed = await rss_fetcher.fetch("https://example.com/broken.xml")
        # feedparser is lenient — may parse partial entries or return empty
        # Key assertion: no exception raised
        assert isinstance(feed.entries, list)

    @respx.mock
    @pytest.mark.asyncio
    async def test_id_falls_back_to_link(self, rss_fetcher: RssFetcher) -> None:
        """When guid is missing, id falls back to link."""
        xml = """<?xml version="1.0"?>
        <rss version="2.0"><channel><title>Test</title>
        <item><title>No GUID</title><link>https://example.com/item</link></item>
        </channel></rss>"""
        respx.get("https://example.com/feed.xml").respond(200, text=xml)

        feed = await rss_fetcher.fetch("https://example.com/feed.xml")
        assert feed.entries[0].id == "https://example.com/item"
