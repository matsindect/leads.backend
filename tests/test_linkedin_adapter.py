"""Tests for LinkedInAdapter.normalize() and helpers — no Playwright needed."""

from __future__ import annotations

from datetime import datetime, timezone
from unittest.mock import AsyncMock

import pytest

from config import Settings
from domain.models import SignalType
from modules.scraping.adapters.linkedin import (
    LinkedInAdapter,
    _is_auth_wall,
    _parse_relative_time,
)


@pytest.fixture
def adapter(settings: Settings) -> LinkedInAdapter:
    mock_fetcher = AsyncMock()
    return LinkedInAdapter(fetcher=mock_fetcher, settings=settings)


class TestLinkedInNormalize:

    def test_basic_listing(self, adapter: LinkedInAdapter) -> None:
        raw = {
            "title": "Senior Python Developer",
            "company": "TechCorp",
            "location": "Remote",
            "posted_time": "2 hours ago",
            "url": "https://linkedin.com/jobs/view/12345",
            "description": "Python FastAPI Docker experience required.",
        }
        lead = adapter.normalize(raw)
        assert lead is not None
        assert lead.signal_type == SignalType.HIRING
        assert lead.signal_strength == 80
        assert lead.source == "linkedin"
        assert lead.company_name == "TechCorp"

    def test_stack_extraction(self, adapter: LinkedInAdapter) -> None:
        raw = {
            "title": "Backend Engineer",
            "company": "StartupX",
            "url": "https://linkedin.com/jobs/1",
            "description": "We use Python, FastAPI, PostgreSQL, and Kubernetes.",
            "posted_time": "", "location": "",
        }
        lead = adapter.normalize(raw)
        assert lead is not None
        assert "python" in lead.stack_mentions
        assert "kubernetes" in lead.stack_mentions

    def test_empty_title_returns_none(self, adapter: LinkedInAdapter) -> None:
        raw = {"title": "", "company": "", "url": "", "description": "", "posted_time": ""}
        assert adapter.normalize(raw) is None

    def test_posted_at_parsed(self, adapter: LinkedInAdapter) -> None:
        raw = {
            "title": "Developer", "company": "Co",
            "url": "https://linkedin.com/jobs/1",
            "description": "", "location": "",
            "posted_time": "3 hours ago",
        }
        lead = adapter.normalize(raw)
        assert lead is not None
        assert lead.posted_at is not None
        assert lead.posted_at.tzinfo is not None


class TestParseRelativeTime:

    def test_hours_ago(self) -> None:
        result = _parse_relative_time("2 hours ago")
        assert result is not None
        assert (datetime.now(timezone.utc) - result).total_seconds() < 7300

    def test_days_ago(self) -> None:
        result = _parse_relative_time("3 days ago")
        assert result is not None
        assert (datetime.now(timezone.utc) - result).total_seconds() > 200000

    def test_empty_returns_none(self) -> None:
        assert _parse_relative_time("") is None

    def test_unparseable_returns_none(self) -> None:
        assert _parse_relative_time("just now") is None


class TestAuthWallDetection:

    def test_detects_auth_wall(self) -> None:
        html = '<html><body>Please Sign in to LinkedIn to continue</body></html>'
        assert _is_auth_wall(html) is True

    def test_normal_page(self) -> None:
        html = '<html><body><div class="jobs-search__results-list">listings</div></body></html>'
        assert _is_auth_wall(html) is False
