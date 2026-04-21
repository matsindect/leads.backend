"""Tests for HNHiringAdapter.normalize() — pure function, no I/O needed."""

from __future__ import annotations

import httpx
import pytest

from config import Settings
from domain.models import SignalType
from infrastructure.fetchers.http import HttpFetcher
from modules.scraping.adapters.hnhiring import HNHiringAdapter
from modules.scraping.signals import DEFAULT_CLASSIFIER


@pytest.fixture
def adapter(settings: Settings) -> HNHiringAdapter:
    client = httpx.AsyncClient()
    fetcher = HttpFetcher(client, user_agent="test/1.0")
    return HNHiringAdapter(fetcher=fetcher, settings=settings)


def _sample_comment(
    *, text: str, object_id: str = "12345", author: str = "ceo_jane"
) -> dict:
    """Build a realistic Algolia comment hit."""
    return {
        "objectID": object_id,
        "author": author,
        "parent_id": 99999999,  # thread root
        "story_id": 99999999,
        "comment_text": text,
        "created_at": "2026-04-01T12:00:00.000Z",
    }


class TestHNHiringNormalize:
    """Verify the pure normalize() method."""

    def test_basic_hiring_post(self, adapter: HNHiringAdapter) -> None:
        """Top-level comment → HIRING signal, strength 90."""
        raw = _sample_comment(
            text=(
                "Acme Corp | Senior Python Developer | Remote | $150k-$200k\n"
                "<p>We&#x27;re building an AI platform with FastAPI and React. "
                "Apply at jobs@acme.io</p>"
            ),
        )
        lead = adapter.normalize(raw, DEFAULT_CLASSIFIER)
        assert lead is not None
        assert lead.source == "hnhiring"
        assert lead.signal_type == SignalType.HIRING
        assert lead.signal_strength == 90
        assert lead.source_id == "12345"

    def test_company_extracted_pipe_format(
        self, adapter: HNHiringAdapter
    ) -> None:
        raw = _sample_comment(
            text="Acme Corp | Senior Engineer | SF, CA | contact@acme.io"
        )
        lead = adapter.normalize(raw, DEFAULT_CLASSIFIER)
        assert lead is not None
        assert lead.company_name == "Acme Corp"

    def test_company_extracted_colon_format(
        self, adapter: HNHiringAdapter
    ) -> None:
        raw = _sample_comment(
            text="StartupX: Looking for a senior dev\nRemote, contact hi@startupx.com"
        )
        lead = adapter.normalize(raw, DEFAULT_CLASSIFIER)
        assert lead is not None
        assert lead.company_name == "StartupX"

    def test_domain_from_email(self, adapter: HNHiringAdapter) -> None:
        raw = _sample_comment(
            text="NewCo | Dev | Remote\nEmail jobs@newco.io if interested."
        )
        lead = adapter.normalize(raw, DEFAULT_CLASSIFIER)
        assert lead is not None
        assert lead.company_domain == "newco.io"

    def test_generic_email_ignored(self, adapter: HNHiringAdapter) -> None:
        """Gmail/Yahoo shouldn't be treated as a company domain."""
        raw = _sample_comment(
            text=(
                "SoloFounder | Dev wanted\n"
                "Reach out: soloFounder@gmail.com or check https://solofounder.io"
            )
        )
        lead = adapter.normalize(raw, DEFAULT_CLASSIFIER)
        assert lead is not None
        # Should prefer the URL domain since gmail is generic
        assert lead.company_domain == "solofounder.io"

    def test_html_stripped(self, adapter: HNHiringAdapter) -> None:
        raw = _sample_comment(
            text=(
                "<p>Acme | Senior Python Dev | Remote</p>"
                "<p>We use <b>FastAPI</b> and <i>React</i>.</p>"
            )
        )
        lead = adapter.normalize(raw, DEFAULT_CLASSIFIER)
        assert lead is not None
        assert "<p>" not in lead.body
        assert "<b>" not in lead.body

    def test_stack_extraction(self, adapter: HNHiringAdapter) -> None:
        raw = _sample_comment(
            text=(
                "DevCo | Backend\nPython, FastAPI, PostgreSQL, Docker, "
                "Kubernetes required."
            )
        )
        lead = adapter.normalize(raw, DEFAULT_CLASSIFIER)
        assert lead is not None
        assert "python" in lead.keywords
        assert "fastapi" in lead.keywords
        assert "docker" in lead.keywords
        assert "kubernetes" in lead.keywords

    def test_author_preserved(self, adapter: HNHiringAdapter) -> None:
        raw = _sample_comment(
            text="Acme | Dev | Remote", author="acme_ceo"
        )
        lead = adapter.normalize(raw, DEFAULT_CLASSIFIER)
        assert lead is not None
        assert lead.person_name == "acme_ceo"

    def test_empty_comment_returns_none(
        self, adapter: HNHiringAdapter
    ) -> None:
        raw = _sample_comment(text="")
        assert adapter.normalize(raw, DEFAULT_CLASSIFIER) is None

    def test_url_constructed(self, adapter: HNHiringAdapter) -> None:
        raw = _sample_comment(
            text="Acme | Dev | Remote", object_id="7654321"
        )
        lead = adapter.normalize(raw, DEFAULT_CLASSIFIER)
        assert lead is not None
        assert lead.url == "https://news.ycombinator.com/item?id=7654321"

    def test_body_capped(self, adapter: HNHiringAdapter) -> None:
        raw = _sample_comment(
            text="BigCo | Dev\n" + "x" * 10000
        )
        lead = adapter.normalize(raw, DEFAULT_CLASSIFIER)
        assert lead is not None
        assert len(lead.body) <= 5000

    def test_title_is_first_line(self, adapter: HNHiringAdapter) -> None:
        raw = _sample_comment(
            text=(
                "Acme Corp | Senior Python Developer | Remote | $150k\n"
                "We are building the future of everything."
            )
        )
        lead = adapter.normalize(raw, DEFAULT_CLASSIFIER)
        assert lead is not None
        assert "Acme Corp" in lead.title
        assert "Senior Python Developer" in lead.title
        assert "future of everything" not in lead.title
