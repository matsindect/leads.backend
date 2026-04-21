"""Tests for WellfoundAdapter.normalize() — pure function, no Playwright needed."""

from __future__ import annotations

from unittest.mock import AsyncMock

import pytest

from config import Settings
from domain.models import SignalType
from modules.scraping.adapters.wellfound import WellfoundAdapter
from modules.scraping.signals import DEFAULT_CLASSIFIER


@pytest.fixture
def adapter(settings: Settings) -> WellfoundAdapter:
    mock_fetcher = AsyncMock()
    return WellfoundAdapter(fetcher=mock_fetcher, settings=settings)


class TestWellfoundNormalize:

    def test_basic_listing(self, adapter: WellfoundAdapter) -> None:
        raw = {
            "title": "Senior Python Developer",
            "company": "Acme Startup",
            "location": "Remote",
            "url": "https://wellfound.com/jobs/12345",
            "tags": "Python FastAPI Docker Kubernetes",
        }
        lead = adapter.normalize(raw, DEFAULT_CLASSIFIER)
        assert lead is not None
        assert lead.signal_type == SignalType.HIRING
        assert lead.signal_strength == 75
        assert lead.source == "wellfound"
        assert lead.company_name == "Acme Startup"
        assert "Acme Startup" in lead.title
        assert "Senior Python Developer" in lead.title

    def test_stack_extraction_from_tags(self, adapter: WellfoundAdapter) -> None:
        raw = {
            "title": "Backend Engineer",
            "company": "DevCo",
            "url": "https://wellfound.com/jobs/99",
            "tags": "React TypeScript Node PostgreSQL AWS",
        }
        lead = adapter.normalize(raw, DEFAULT_CLASSIFIER)
        assert lead is not None
        assert "react" in lead.keywords
        assert "typescript" in lead.keywords
        assert "postgres" in lead.keywords

    def test_empty_title_returns_none(self, adapter: WellfoundAdapter) -> None:
        raw = {"title": "", "company": "", "url": "", "tags": ""}
        assert adapter.normalize(raw, DEFAULT_CLASSIFIER) is None

    def test_location_preserved(self, adapter: WellfoundAdapter) -> None:
        raw = {
            "title": "Engineer",
            "company": "Co",
            "location": "San Francisco, CA",
            "url": "https://wellfound.com/jobs/1",
            "tags": "",
        }
        lead = adapter.normalize(raw, DEFAULT_CLASSIFIER)
        assert lead is not None
        assert lead.location == "San Francisco, CA"

    def test_domain_from_raw(self, adapter: WellfoundAdapter) -> None:
        raw = {
            "title": "Developer",
            "company": "NewCo",
            "url": "https://wellfound.com/jobs/5",
            "tags": "",
            "domain": "newco.io",
        }
        lead = adapter.normalize(raw, DEFAULT_CLASSIFIER)
        assert lead is not None
        assert lead.company_domain == "newco.io"
