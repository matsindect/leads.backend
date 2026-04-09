"""Test for the PendingLeadsResweeper."""

from __future__ import annotations

import uuid
from collections.abc import AsyncIterator
from datetime import UTC, datetime, timedelta

import pytest
import pytest_asyncio
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine
from testcontainers.postgres import PostgresContainer

from application.bus import EventBus
from application.workers import PendingLeadsResweeper
from domain.events import LeadCreated
from infrastructure.postgres_repo import PostgresLeadRepository, metadata


@pytest.fixture(scope="module")
def postgres_url() -> str:
    with PostgresContainer("postgres:16-alpine") as pg:
        sync_url = pg.get_connection_url()
        async_url = sync_url.replace("psycopg2", "asyncpg")
        yield async_url  # type: ignore[misc]


@pytest_asyncio.fixture
async def db_session_factory(postgres_url: str) -> AsyncIterator[async_sessionmaker[AsyncSession]]:
    engine = create_async_engine(postgres_url)
    async with engine.begin() as conn:
        await conn.run_sync(metadata.create_all)
    factory = async_sessionmaker(engine, expire_on_commit=False)
    yield factory
    await engine.dispose()


@pytest.mark.asyncio
async def test_resweeper_republishes_stuck_leads(
    db_session_factory: async_sessionmaker[AsyncSession],
) -> None:
    """Insert a pending_enrichment lead older than threshold, verify resweep."""
    lead_id = uuid.uuid4()

    # Insert a lead stuck in pending_enrichment for > 10 minutes
    old_time = datetime.now(UTC) - timedelta(minutes=15)
    async with db_session_factory() as session, session.begin():
        await session.execute(
            text("""
                INSERT INTO raw_leads (id, source, source_id, dedup_hash, url, title, body,
                    raw_payload, status, fetched_at)
                VALUES (:id, 'test', :sid, :hash, 'https://example.com', 'Test',
                    'Body', '{}', 'pending_enrichment', :fetched)
            """),
            {
                "id": lead_id,
                "sid": f"resweep_{lead_id.hex[:8]}",
                "hash": uuid.uuid4().hex,
                "fetched": old_time,
            },
        )

    repository = PostgresLeadRepository(db_session_factory)
    bus = EventBus(max_queue_size=100)

    resweeper = PendingLeadsResweeper(
        repository=repository,
        bus=bus,
        interval_seconds=300,
        older_than_minutes=10,
    )

    # Run one sweep
    await resweeper._sweep()

    # Verify a LeadCreated event was published
    assert bus.queue_size(LeadCreated) == 1
