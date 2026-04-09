"""Integration test — full scrape cycle against real Postgres.

Uses testcontainers to spin up an ephemeral Postgres instance.
HTTP responses are mocked with httpx's MockTransport.
Events are published to the in-process EventBus (no Redis Streams).
"""

from __future__ import annotations

import uuid
from collections.abc import AsyncIterator
from typing import Any

import httpx
import pytest
import pytest_asyncio
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine
from testcontainers.postgres import PostgresContainer

from application.bus import EventBus
from config import Settings
from domain.events import LeadCreated
from infrastructure.bus_publisher import BusEventPublisher
from infrastructure.postgres_repo import PostgresLeadRepository, metadata
from modules.scraping.adapters.reddit import RedditAdapter
from modules.scraping.orchestrator import ScrapeOrchestrator


def _fake_reddit_response() -> dict[str, Any]:
    """Build a minimal Reddit /new.json response."""
    return {
        "data": {
            "children": [
                {
                    "data": {
                        "id": f"integ_{uuid.uuid4().hex[:8]}",
                        "title": "We're hiring Python developers at acme.io",
                        "selftext": "Looking for someone with fastapi and docker experience.",
                        "author": "test_user",
                        "permalink": "/r/startups/comments/integ/test/",
                        "url": "https://www.reddit.com/r/startups/comments/integ/test/",
                        "created_utc": 1712448000.0,
                        "subreddit": "startups",
                    }
                },
                {
                    "data": {
                        "id": f"nosig_{uuid.uuid4().hex[:8]}",
                        "title": "Beautiful sunset",
                        "selftext": "No signal here.",
                        "author": "tourist",
                        "permalink": "/r/pics/comments/nosig/sunset/",
                        "url": "https://www.reddit.com/r/pics/comments/nosig/sunset/",
                        "created_utc": 1712448000.0,
                        "subreddit": "startups",
                    }
                },
            ]
        }
    }


def _mock_transport(request: httpx.Request) -> httpx.Response:
    """Return fake Reddit API responses."""
    return httpx.Response(
        status_code=200,
        json=_fake_reddit_response(),
        request=request,
    )


@pytest.fixture(scope="module")
def postgres_url() -> str:
    """Start a Postgres container and return its async connection URL."""
    with PostgresContainer("postgres:16-alpine") as pg:
        # testcontainers returns psycopg2 URL; convert to asyncpg
        sync_url = pg.get_connection_url()
        async_url = sync_url.replace("psycopg2", "asyncpg")
        yield async_url  # type: ignore[misc]


@pytest_asyncio.fixture
async def db_session_factory(postgres_url: str) -> AsyncIterator[async_sessionmaker[AsyncSession]]:
    """Create tables and return a session factory."""
    engine = create_async_engine(postgres_url)
    async with engine.begin() as conn:
        await conn.run_sync(metadata.create_all)
    factory = async_sessionmaker(engine, expire_on_commit=False)
    yield factory
    await engine.dispose()


@pytest.mark.asyncio
async def test_full_scrape_integration(
    db_session_factory: async_sessionmaker[AsyncSession],
) -> None:
    """Run a complete scrape cycle against real Postgres with in-process EventBus."""
    settings = Settings(
        reddit_subreddits=["startups"],
    )

    # Build components with mocked HTTP and in-process bus
    http_client = httpx.AsyncClient(transport=httpx.MockTransport(_mock_transport))
    event_bus = EventBus(max_queue_size=100)
    repository = PostgresLeadRepository(db_session_factory)
    publisher = BusEventPublisher(event_bus)
    orchestrator = ScrapeOrchestrator(
        repository=repository,
        publisher=publisher,
        settings=settings,
    )
    adapter = RedditAdapter(client=http_client, settings=settings)

    # Run the scrape
    report = await orchestrator.run(adapter)

    # Assertions on the report
    assert report.adapter_name == "reddit"
    assert report.fetched == 2
    assert report.normalized >= 1  # at least the hiring post
    assert report.inserted >= 1
    assert report.error is None

    # Verify data persisted in Postgres
    async with db_session_factory() as session:
        result = await session.execute(text("SELECT COUNT(*) FROM raw_leads"))
        count = result.scalar_one()
        assert count >= 1

        result = await session.execute(text("SELECT COUNT(*) FROM scrape_runs"))
        run_count = result.scalar_one()
        assert run_count == 1

    # Verify events published to the in-process bus
    assert event_bus.queue_size(LeadCreated) >= 1

    # Verify idempotency — running again should produce duplicates
    report2 = await orchestrator.run(adapter)
    assert report2.duplicates >= 1 or report2.inserted == 0

    await http_client.aclose()
