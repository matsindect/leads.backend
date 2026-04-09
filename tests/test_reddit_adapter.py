"""Tests for RedditAdapter.normalize() — pure function, no I/O needed."""

from __future__ import annotations

import httpx
import pytest

from config import Settings
from domain.models import SignalType
from infrastructure.fetchers.http import HttpFetcher
from modules.scraping.adapters.reddit import RedditAdapter


@pytest.fixture
def adapter(settings: Settings) -> RedditAdapter:
    """Create a RedditAdapter with a dummy fetcher (normalize is pure)."""
    client = httpx.AsyncClient()
    fetcher = HttpFetcher(client, user_agent="test/1.0")
    return RedditAdapter(fetcher=fetcher, settings=settings)


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
            "id": "xyz999",
            "title": "Beautiful sunset photo",
            "selftext": "Taken in Hawaii last week.",
            "author": "tourist",
            "permalink": "/r/pics/comments/xyz999/sunset/",
            "created_utc": 1712448000.0,
        }
        lead = adapter.normalize(raw)
        assert lead is None

    def test_url_construction(
        self, adapter: RedditAdapter, sample_reddit_raw_post: dict
    ) -> None:
        """URL should be built from the permalink."""
        lead = adapter.normalize(sample_reddit_raw_post)
        assert lead is not None
        assert lead.url == "https://www.reddit.com/r/startups/comments/abc123/were_hiring/"

    def test_posted_at_parsed(
        self, adapter: RedditAdapter, sample_reddit_raw_post: dict
    ) -> None:
        """created_utc should be converted to a timezone-aware datetime."""
        lead = adapter.normalize(sample_reddit_raw_post)
        assert lead is not None
        assert lead.posted_at is not None
        assert lead.posted_at.tzinfo is not None

    def test_body_capped(self, adapter: RedditAdapter) -> None:
        """Very long body text should be truncated."""
        raw = {
            "id": "long1",
            "title": "Evaluating alternatives to our current stack",
            "selftext": "x" * 10000,
            "author": "dev",
            "permalink": "/r/webdev/comments/long1/eval/",
            "created_utc": 1712448000.0,
        }
        lead = adapter.normalize(raw)
        assert lead is not None
        assert len(lead.body) <= 5000

    def test_pain_point_signal(self, adapter: RedditAdapter) -> None:
        """Post with pain point keywords should be classified correctly."""
        raw = {
            "id": "pain1",
            "title": "Struggling with our CI/CD pipeline",
            "selftext": "It's broken and frustrating to deal with.",
            "author": "devops_eng",
            "permalink": "/r/devops/comments/pain1/ci/",
            "created_utc": 1712448000.0,
        }
        lead = adapter.normalize(raw)
        assert lead is not None
        assert lead.signal_type == SignalType.PAIN_POINT
