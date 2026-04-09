"""Shared test fixtures."""

from __future__ import annotations

from datetime import UTC, datetime

import pytest

from config import Settings
from domain.models import CanonicalLead, SignalType


@pytest.fixture
def settings() -> Settings:
    """Provide default settings for unit tests."""
    return Settings()


@pytest.fixture
def sample_reddit_raw_post() -> dict:
    """A realistic raw Reddit post dictionary."""
    return {
        "id": "abc123",
        "name": "t3_abc123",
        "title": "We're hiring a senior Python developer for our growing SaaS startup",
        "selftext": (
            "Our company at acme.io is expanding and we need help with our "
            "FastAPI backend and React frontend. Budget is around $150k. "
            "We use postgres, docker, and kubernetes in production."
        ),
        "author": "startup_founder",
        "permalink": "/r/startups/comments/abc123/were_hiring/",
        "url": "https://www.reddit.com/r/startups/comments/abc123/were_hiring/",
        "created_utc": 1712448000.0,  # 2024-04-07 00:00:00 UTC
        "subreddit": "startups",
        "_subreddit": "startups",
    }


@pytest.fixture
def sample_canonical_lead() -> CanonicalLead:
    """A pre-built canonical lead for testing."""
    return CanonicalLead(
        source="reddit",
        source_id="abc123",
        url="https://www.reddit.com/r/startups/comments/abc123/were_hiring/",
        title="We're hiring a senior Python developer",
        body="Our company is expanding...",
        raw_payload={"id": "abc123"},
        signal_type=SignalType.HIRING,
        signal_strength=60,
        company_domain="acme.io",
        person_name="startup_founder",
        stack_mentions=["python", "fastapi"],
        posted_at=datetime(2024, 4, 7, tzinfo=UTC),
    )


@pytest.fixture
def sample_canonical_lead_no_domain() -> CanonicalLead:
    """A lead with no company_domain — tests fallback dedup strategies."""
    return CanonicalLead(
        source="reddit",
        source_id="def456",
        url="https://www.reddit.com/r/webdev/comments/def456/help/",
        title="Struggling with our deployment pipeline",
        body="Looking for alternatives to Jenkins...",
        raw_payload={"id": "def456"},
        signal_type=SignalType.PAIN_POINT,
        signal_strength=70,
        person_name="DevOps Dan",
        stack_mentions=["jenkins", "docker"],
        posted_at=datetime(2024, 4, 7, tzinfo=UTC),
    )


@pytest.fixture
def sample_canonical_lead_minimal() -> CanonicalLead:
    """A lead with only a URL — tests final fallback dedup."""
    return CanonicalLead(
        source="reddit",
        source_id="ghi789",
        url="https://www.reddit.com/r/SaaS/comments/ghi789/recommend/",
        title="Can anyone recommend a good CRM?",
        body="",
        raw_payload={"id": "ghi789"},
        signal_type=SignalType.GENERAL_INTEREST,
        signal_strength=30,
    )
