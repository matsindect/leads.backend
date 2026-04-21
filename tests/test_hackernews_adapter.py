"""Tests for HackerNewsAdapter.normalize() — pure function."""

from __future__ import annotations

import httpx
import pytest

from config import Settings
from domain.models import SignalType
from infrastructure.fetchers.http import HttpFetcher
from modules.scraping.adapters.hackernews import HackerNewsAdapter
from modules.scraping.signals import DEFAULT_CLASSIFIER


@pytest.fixture
def adapter(settings: Settings) -> HackerNewsAdapter:
    client = httpx.AsyncClient()
    fetcher = HttpFetcher(client, user_agent="test/1.0")
    return HackerNewsAdapter(fetcher=fetcher, settings=settings)


class TestHackerNewsNormalize:
    """Verify the normalize() pure function."""

    def test_hiring_signal(self, adapter: HackerNewsAdapter) -> None:
        raw = {
            "objectID": "12345",
            "title": "We're hiring senior Python engineers at acme.io",
            "story_text": "Looking for fastapi and docker experience.",
            "author": "hn_user",
            "url": "https://acme.io/careers",
            "created_at": "2024-04-07T12:00:00.000Z",
        }
        lead = adapter.normalize(raw, DEFAULT_CLASSIFIER)
        assert lead is not None
        assert lead.signal_type == SignalType.HIRING
        assert lead.source == "hackernews"
        assert lead.source_id == "12345"

    def test_no_signal_returns_none(self, adapter: HackerNewsAdapter) -> None:
        raw = {
            "objectID": "99999",
            "title": "A beautiful day",
            "story_text": "Nothing relevant here.",
            "author": "tourist",
            "created_at": "2024-04-07T12:00:00.000Z",
        }
        assert adapter.normalize(raw, DEFAULT_CLASSIFIER) is None

    def test_stack_extraction(self, adapter: HackerNewsAdapter) -> None:
        raw = {
            "objectID": "11111",
            "title": "Evaluating alternatives to our current kubernetes setup",
            "story_text": "We use docker and postgres heavily.",
            "author": "devops",
            "created_at": "2024-04-07T12:00:00.000Z",
        }
        lead = adapter.normalize(raw, DEFAULT_CLASSIFIER)
        assert lead is not None
        assert "kubernetes" in lead.keywords
        assert "docker" in lead.keywords
        assert "postgres" in lead.keywords

    def test_domain_extraction(self, adapter: HackerNewsAdapter) -> None:
        raw = {
            "objectID": "22222",
            "title": "We're hiring at startup.io",
            "story_text": "",
            "author": "founder",
            "created_at": "2024-04-07T12:00:00.000Z",
        }
        lead = adapter.normalize(raw, DEFAULT_CLASSIFIER)
        assert lead is not None
        assert lead.company_domain == "startup.io"

    def test_fallback_url(self, adapter: HackerNewsAdapter) -> None:
        """When url is absent, construct HN item link from objectID."""
        raw = {
            "objectID": "33333",
            "title": "Recommend a good CI tool",
            "story_text": "",
            "author": "dev",
            "created_at": "2024-04-07T12:00:00.000Z",
        }
        lead = adapter.normalize(raw, DEFAULT_CLASSIFIER)
        assert lead is not None
        assert lead.url == "https://news.ycombinator.com/item?id=33333"

    def test_timestamp_parsing(self, adapter: HackerNewsAdapter) -> None:
        raw = {
            "objectID": "44444",
            "title": "Struggling with our deploy pipeline",
            "story_text": "",
            "author": "eng",
            "created_at": "2024-04-07T15:30:00.000Z",
        }
        lead = adapter.normalize(raw, DEFAULT_CLASSIFIER)
        assert lead is not None
        assert lead.posted_at is not None
        assert lead.posted_at.tzinfo is not None
