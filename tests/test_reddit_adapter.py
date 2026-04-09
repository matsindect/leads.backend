"""Tests for RedditAdapter.normalize() — pure function, no I/O needed."""

from __future__ import annotations

from datetime import UTC, datetime

import httpx
import pytest

from config import Settings
from domain.models import SignalType
from infrastructure.fetchers.http import HttpFetcher
from infrastructure.fetchers.rss import RssFetcher
from modules.scraping.adapters.reddit import RedditAdapter


@pytest.fixture
def adapter(settings: Settings) -> RedditAdapter:
    """Create a RedditAdapter with a dummy fetcher (normalize is pure)."""
    client = httpx.AsyncClient()
    http = HttpFetcher(client, user_agent="test/1.0")
    rss = RssFetcher(http)
    return RedditAdapter(fetcher=rss, settings=settings)


class TestRedditNormalize:
    """Verify the pure normalize() method with fixture data."""

    def test_hiring_signal_detected(
        self, adapter: RedditAdapter, sample_reddit_raw_post: dict
    ) -> None:
        """Post mentioning 'hiring' should be classified as HIRING."""
        lead = adapter.normalize(sample_reddit_raw_post)
        assert lead is not None
        assert lead.signal_type == SignalType.HIRING
        assert lead.source == "reddit"
        assert lead.source_id == "abc123"

    def test_stack_mentions_extracted(
        self, adapter: RedditAdapter, sample_reddit_raw_post: dict
    ) -> None:
        """Technology keywords in text should be captured."""
        lead = adapter.normalize(sample_reddit_raw_post)
        assert lead is not None
        assert "fastapi" in lead.stack_mentions
        assert "postgres" in lead.stack_mentions
        assert "docker" in lead.stack_mentions
        assert "kubernetes" in lead.stack_mentions
        assert "react" in lead.stack_mentions

    def test_company_domain_extracted(
        self, adapter: RedditAdapter, sample_reddit_raw_post: dict
    ) -> None:
        """Domain pattern 'at acme.io' should be captured."""
        lead = adapter.normalize(sample_reddit_raw_post)
        assert lead is not None
        assert lead.company_domain == "acme.io"

    def test_no_signal_returns_none(self, adapter: RedditAdapter) -> None:
        """Post with no signal keywords should be dropped."""
        raw = {
            "id": "t3_xyz999",
            "title": "Beautiful sunset photo",
            "summary": "<p>Taken in Hawaii last week.</p>",
            "link": "https://www.reddit.com/r/pics/comments/xyz999/sunset/",
            "author": "/user/tourist",
            "published_at": datetime(2024, 4, 7, tzinfo=UTC),
        }
        lead = adapter.normalize(raw)
        assert lead is None

    def test_url_construction(
        self, adapter: RedditAdapter, sample_reddit_raw_post: dict
    ) -> None:
        """URL is the entry's link."""
        lead = adapter.normalize(sample_reddit_raw_post)
        assert lead is not None
        assert lead.url == "https://www.reddit.com/r/startups/comments/abc123/were_hiring/"

    def test_posted_at_parsed(
        self, adapter: RedditAdapter, sample_reddit_raw_post: dict
    ) -> None:
        """published_at should be carried through as timezone-aware datetime."""
        lead = adapter.normalize(sample_reddit_raw_post)
        assert lead is not None
        assert lead.posted_at is not None
        assert lead.posted_at.tzinfo is not None

    def test_author_username_extracted(
        self, adapter: RedditAdapter, sample_reddit_raw_post: dict
    ) -> None:
        """Author '/user/name' format should be normalized to just 'name'."""
        lead = adapter.normalize(sample_reddit_raw_post)
        assert lead is not None
        assert lead.person_name == "startup_founder"

    def test_html_stripped_from_body(
        self, adapter: RedditAdapter, sample_reddit_raw_post: dict
    ) -> None:
        """RSS summary HTML tags should be stripped from body."""
        lead = adapter.normalize(sample_reddit_raw_post)
        assert lead is not None
        assert "<p>" not in lead.body
        assert "</p>" not in lead.body

    def test_body_capped(self, adapter: RedditAdapter) -> None:
        """Very long body text should be truncated."""
        raw = {
            "id": "t3_long1",
            "title": "Evaluating alternatives to our current stack",
            "summary": "<p>" + ("x" * 10000) + "</p>",
            "link": "https://www.reddit.com/r/webdev/comments/long1/eval/",
            "author": "/user/dev",
            "published_at": datetime(2024, 4, 7, tzinfo=UTC),
        }
        lead = adapter.normalize(raw)
        assert lead is not None
        assert len(lead.body) <= 5000

    def test_pain_point_signal(self, adapter: RedditAdapter) -> None:
        """Post with pain point keywords should be classified correctly."""
        raw = {
            "id": "t3_pain1",
            "title": "Struggling with our CI/CD pipeline",
            "summary": "<p>It's broken and frustrating to deal with.</p>",
            "link": "https://www.reddit.com/r/devops/comments/pain1/ci/",
            "author": "/user/devops_eng",
            "published_at": datetime(2024, 4, 7, tzinfo=UTC),
        }
        lead = adapter.normalize(raw)
        assert lead is not None
        assert lead.signal_type == SignalType.PAIN_POINT
