"""Integration test — enrichment pipeline against real Postgres.

Uses testcontainers for Postgres.  LLM is stubbed with canned responses.
Verifies the full pipeline: insert lead → publish event → enrich → persist.
"""

from __future__ import annotations

import uuid
from collections.abc import AsyncIterator
from datetime import UTC, datetime
from typing import Any

import httpx
import pytest
import pytest_asyncio
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine
from testcontainers.postgres import PostgresContainer

from application.bus import EventBus
from config import Settings
from domain.events import LeadScored
from domain.interfaces import ModelHint
from domain.models import AlreadyProcessedError
from infrastructure.postgres_repo import PostgresLeadRepository, metadata
from infrastructure.prompt_loader import PromptLoader
from modules.enrichment.company_resolver import LLMCompanyResolver
from modules.enrichment.pipeline import EnrichmentPipeline

CANNED_CLASSIFICATION = {
    "refined_signal_type": "hiring",
    "refined_signal_strength": 75,
    "company_stage": "startup",
    "decision_maker_likelihood": 80,
    "urgency_score": 60,
    "icp_fit_score": 85,
    "extracted_stack": ["python", "fastapi"],
    "pain_summary": "Growing team needs senior engineers",
    "recommended_approach": "cold_dm",
    "skip_reason": None,
    "_usage": {"input_tokens": 500, "output_tokens": 200, "model": "test-model"},
}

CANNED_RESOLUTION = {
    "company_name": "Test Corp",
    "company_domain": "testcorp.io",
    "_usage": {"input_tokens": 100, "output_tokens": 50, "model": "test-cheap"},
}


class StubLLMProvider:
    """Returns canned responses based on model_hint."""

    async def complete_structured(
        self, prompt: str, schema: dict[str, Any], model_hint: ModelHint
    ) -> dict[str, Any]:
        if model_hint == ModelHint.CHEAP:
            return CANNED_RESOLUTION.copy()
        return CANNED_CLASSIFICATION.copy()


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


async def _insert_fake_lead(
    session_factory: async_sessionmaker[AsyncSession],
) -> uuid.UUID:
    """Insert a raw lead and return its ID."""
    lead_id = uuid.uuid4()
    async with session_factory() as session, session.begin():
        await session.execute(
            text("""
                INSERT INTO raw_leads (id, source, source_id, dedup_hash, url, title, body,
                    raw_payload, signal_type, signal_strength, status, stack_mentions,
                    company_domain, person_name, posted_at)
                VALUES (:id, 'test', :sid, :hash, 'https://example.com', 'Hiring Python devs',
                    'Looking for engineers at acme.io', '{}', 'hiring', 60, 'new',
                    ARRAY['python', 'fastapi'], 'acme.io', 'test_user', :posted)
            """),
            {
                "id": lead_id,
                "sid": f"test_{lead_id.hex[:8]}",
                "hash": uuid.uuid4().hex,
                "posted": datetime.now(UTC),
            },
        )
    return lead_id


@pytest.mark.asyncio
async def test_enrichment_pipeline_integration(
    db_session_factory: async_sessionmaker[AsyncSession],
) -> None:
    """Full pipeline: insert lead → enrich → verify DB state."""
    settings = Settings()
    event_bus = EventBus(max_queue_size=100)
    repository = PostgresLeadRepository(db_session_factory)
    llm = StubLLMProvider()
    prompt_loader = PromptLoader()
    resolver = LLMCompanyResolver(llm=llm, repository=repository, prompt_loader=prompt_loader)
    http_client = httpx.AsyncClient()

    pipeline = EnrichmentPipeline(
        repository=repository,
        llm=llm,
        resolver=resolver,
        prompt_loader=prompt_loader,
        http_client=http_client,
        event_bus=event_bus,
        settings=settings,
    )

    # Insert a fake lead
    lead_id = await _insert_fake_lead(db_session_factory)

    # Run the pipeline
    context = await pipeline.execute(lead_id)

    # Verify the result
    assert context.final_score is not None
    assert 0 <= context.final_score <= 100
    assert context.classification is not None
    assert context.classification.recommended_approach == "cold_dm"

    # Verify DB state
    async with db_session_factory() as session:
        result = await session.execute(
            text("SELECT status, score FROM raw_leads WHERE id = :id"),
            {"id": lead_id},
        )
        row = result.fetchone()
        assert row is not None
        assert row.status == "scored"
        assert float(row.score) > 0

        # Check lead_enrichments
        result = await session.execute(
            text("SELECT * FROM lead_enrichments WHERE lead_id = :id"),
            {"id": lead_id},
        )
        enrichment = result.fetchone()
        assert enrichment is not None
        assert enrichment.recommended_approach == "cold_dm"

        # Check LLM call log
        result = await session.execute(
            text("SELECT COUNT(*) FROM llm_call_log WHERE lead_id = :id"),
            {"id": lead_id},
        )
        assert result.scalar_one() >= 1

    # Verify LeadScored event published
    assert event_bus.queue_size(LeadScored) == 1

    await http_client.aclose()


@pytest.mark.asyncio
async def test_idempotency(
    db_session_factory: async_sessionmaker[AsyncSession],
) -> None:
    """Running pipeline twice on the same lead should not duplicate data."""
    settings = Settings()
    event_bus = EventBus(max_queue_size=100)
    repository = PostgresLeadRepository(db_session_factory)
    llm = StubLLMProvider()
    prompt_loader = PromptLoader()
    resolver = LLMCompanyResolver(llm=llm, repository=repository, prompt_loader=prompt_loader)
    http_client = httpx.AsyncClient()

    pipeline = EnrichmentPipeline(
        repository=repository,
        llm=llm,
        resolver=resolver,
        prompt_loader=prompt_loader,
        http_client=http_client,
        event_bus=event_bus,
        settings=settings,
    )

    lead_id = await _insert_fake_lead(db_session_factory)

    # First run
    await pipeline.execute(lead_id)

    # Second run — should raise AlreadyProcessedError
    with pytest.raises(AlreadyProcessedError):
        await pipeline.execute(lead_id)

    # Verify exactly one enrichment row
    async with db_session_factory() as session:
        result = await session.execute(
            text("SELECT COUNT(*) FROM lead_enrichments WHERE lead_id = :id"),
            {"id": lead_id},
        )
        assert result.scalar_one() == 1

    await http_client.aclose()
