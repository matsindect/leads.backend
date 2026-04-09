"""Integration test — full scrape cycle against real Postgres.

Uses testcontainers to spin up an ephemeral Postgres instance.
HTTP responses are mocked with httpx's MockTransport.
Events are published to the in-process EventBus (no Redis Streams).
"""

from __future__ import annotations

from collections.abc import AsyncIterator

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
from infrastructure.fetchers.http import HttpFetcher
from infrastructure.fetchers.rss import RssFetcher
from infrastructure.postgres_repo import PostgresLeadRepository, metadata
from modules.scraping.adapters.reddit import RedditAdapter
from modules.scraping.orchestrator import ScrapeOrchestrator

_FAKE_REDDIT_RSS = """<?xml version="1.0" encoding="UTF-8"?>
<feed xmlns="http://www.w3.org/2005/Atom">
  <title>r/startups</title>
  <entry>
    <id>t3_integ001</id>
    <title>We're hiring Python developers at acme.io</title>
    <link href="https://www.reddit.com/r/startups/comments/integ001/test/"/>
    <updated>2024-04-07T12:00:00+00:00</updated>
    <author><name>/user/test_user</name></author>
    <content type="html">
      &lt;p&gt;Looking for fastapi and docker experience.&lt;/p&gt;
    </content>
  </entry>
  <entry>
    <id>t3_nosig01</id>
    <title>Beautiful sunset</title>
    <link href="https://www.reddit.com/r/pics/comments/nosig01/sunset/"/>
    <updated>2024-04-07T11:00:00+00:00</updated>
    <author><name>/user/tourist</name></author>
    <content type="html">&lt;p&gt;No signal here.&lt;/p&gt;</content>
  </entry>
</feed>
"""


def _mock_transport(request: httpx.Request) -> httpx.Response:
    """Return a fake Reddit RSS Atom feed."""
    return httpx.Response(
        status_code=200,
        text=_FAKE_REDDIT_RSS,
        headers={"content-type": "application/atom+xml"},
        request=request,
    )


@pytest.fixture(scope="module")
def postgres_url() -> str:
    """Start a Postgres container and return its async connection URL."""
    with PostgresContainer("postgres:16-alpine") as pg:
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
    settings = Settings(reddit_subreddits=["startups"])

    http_client = httpx.AsyncClient(transport=httpx.MockTransport(_mock_transport))
    http_fetcher = HttpFetcher(http_client, user_agent="test/1.0")
    rss_fetcher = RssFetcher(http_fetcher)

    event_bus = EventBus(max_queue_size=100)
    repository = PostgresLeadRepository(db_session_factory)
    publisher = BusEventPublisher(event_bus)
    orchestrator = ScrapeOrchestrator(
        repository=repository,
        publisher=publisher,
        settings=settings,
    )
    adapter = RedditAdapter(fetcher=rss_fetcher, settings=settings)

    report = await orchestrator.run(adapter)

    assert report.adapter_name == "reddit"
    assert report.fetched == 2
    assert report.normalized >= 1
    assert report.inserted >= 1
    assert report.error is None

    async with db_session_factory() as session:
        result = await session.execute(text("SELECT COUNT(*) FROM raw_leads"))
        count = result.scalar_one()
        assert count >= 1

        result = await session.execute(text("SELECT COUNT(*) FROM scrape_runs"))
        run_count = result.scalar_one()
        assert run_count == 1

    assert event_bus.queue_size(LeadCreated) >= 1

    # Idempotency: second run should produce duplicates
    report2 = await orchestrator.run(adapter)
    assert report2.duplicates >= 1 or report2.inserted == 0

    await http_client.aclose()
