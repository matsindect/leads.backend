"""Tests for ScrapeOrchestrator — happy path with mocked collaborators."""

from __future__ import annotations

import uuid
from datetime import UTC, datetime
from unittest.mock import AsyncMock

import pytest

from config import Settings
from domain.models import (
    CanonicalLead,
    SignalType,
)
from modules.scraping.orchestrator import ScrapeOrchestrator


class FakeAdapter:
    """Minimal SourceAdapter for testing."""

    def __init__(self, raw_data: list[dict], leads: list[CanonicalLead | None]) -> None:
        self._raw = raw_data
        self._leads = leads

    @property
    def name(self) -> str:
        return "fake"

    @property
    def poll_interval_seconds(self) -> int:
        return 60

    async def fetch_raw(self) -> list[dict]:
        return self._raw

    def normalize(self, raw: dict) -> CanonicalLead | None:
        idx = self._raw.index(raw)
        return self._leads[idx] if idx < len(self._leads) else None


def _make_lead(source_id: str) -> CanonicalLead:
    return CanonicalLead(
        source="fake",
        source_id=source_id,
        url=f"https://example.com/{source_id}",
        title="Test lead",
        body="Body",
        raw_payload={"id": source_id},
        signal_type=SignalType.HIRING,
        signal_strength=60,
        posted_at=datetime(2024, 4, 7, tzinfo=UTC),
    )


@pytest.fixture
def mock_repository() -> AsyncMock:
    repo = AsyncMock()
    repo.count_recent_failures = AsyncMock(return_value=0)
    repo.insert_leads = AsyncMock(
        return_value=([uuid.uuid4(), uuid.uuid4()], 0)
    )
    repo.record_run = AsyncMock()
    return repo


@pytest.fixture
def mock_publisher() -> AsyncMock:
    pub = AsyncMock()
    pub.publish_new_leads = AsyncMock()
    return pub


@pytest.fixture
def orchestrator(
    mock_repository: AsyncMock, mock_publisher: AsyncMock, settings: Settings
) -> ScrapeOrchestrator:
    return ScrapeOrchestrator(
        repository=mock_repository,
        publisher=mock_publisher,
        settings=settings,
    )


class TestOrchestratorHappyPath:
    """Verify the full scrape cycle with mocked dependencies."""

    @pytest.mark.asyncio
    async def test_full_cycle(
        self,
        orchestrator: ScrapeOrchestrator,
        mock_repository: AsyncMock,
        mock_publisher: AsyncMock,
    ) -> None:
        """fetch → normalize → insert → publish events → record run."""
        raw = [{"id": "1"}, {"id": "2"}]
        leads = [_make_lead("1"), _make_lead("2")]
        adapter = FakeAdapter(raw, leads)

        report = await orchestrator.run(adapter)

        assert report.adapter_name == "fake"
        assert report.fetched == 2
        assert report.normalized == 2
        assert report.inserted == 2
        assert report.duplicates == 0
        assert report.error is None
        assert report.duration_ms >= 0

        mock_repository.insert_leads.assert_awaited_once()
        mock_publisher.publish_new_leads.assert_awaited_once()
        mock_repository.record_run.assert_awaited_once()

    @pytest.mark.asyncio
    async def test_dropped_posts_not_inserted(
        self,
        orchestrator: ScrapeOrchestrator,
        mock_repository: AsyncMock,
    ) -> None:
        """Posts that normalize to None should not reach the repository."""
        raw = [{"id": "1"}, {"id": "2"}, {"id": "3"}]
        leads: list[CanonicalLead | None] = [_make_lead("1"), None, _make_lead("3")]
        adapter = FakeAdapter(raw, leads)

        report = await orchestrator.run(adapter)

        assert report.fetched == 3
        assert report.normalized == 2
        # insert_leads receives only the non-None leads
        call_args = mock_repository.insert_leads.call_args
        assert len(call_args[0][0]) == 2

    @pytest.mark.asyncio
    async def test_circuit_breaker_blocks_scrape(
        self,
        orchestrator: ScrapeOrchestrator,
        mock_repository: AsyncMock,
    ) -> None:
        """When consecutive failures >= threshold, scrape is skipped."""
        mock_repository.count_recent_failures.return_value = 3
        adapter = FakeAdapter([], [])

        report = await orchestrator.run(adapter)

        assert report.error == "circuit_breaker_open"
        assert report.fetched == 0
        mock_repository.insert_leads.assert_not_awaited()

    @pytest.mark.asyncio
    async def test_fetch_error_recorded(
        self,
        orchestrator: ScrapeOrchestrator,
        mock_repository: AsyncMock,
        mock_publisher: AsyncMock,
    ) -> None:
        """When fetch_raw raises, error is captured in the report."""
        adapter = FakeAdapter([], [])
        adapter.fetch_raw = AsyncMock(side_effect=RuntimeError("network down"))  # type: ignore[method-assign]

        report = await orchestrator.run(adapter)

        assert report.error is not None
        assert "network down" in report.error
        mock_publisher.publish_new_leads.assert_not_awaited()
        mock_repository.record_run.assert_awaited_once()
