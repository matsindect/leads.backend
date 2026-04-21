"""Unit tests for each enrichment pipeline stage with mocked dependencies."""

from __future__ import annotations

import uuid
from datetime import UTC, datetime
from unittest.mock import AsyncMock

import pytest

from domain.models import AlreadyProcessedError, EnrichmentResult, PipelineContext
from modules.enrichment.stages.classify import BudgetExceededError, ClassifyStage
from modules.enrichment.stages.enrich_company import EnrichCompanyStage
from modules.enrichment.stages.fetch import FetchStage
from modules.enrichment.stages.persist import PersistStage
from modules.enrichment.stages.resolve_company import ResolveCompanyStage
from modules.enrichment.stages.score import ScoreStage

LEAD_ID = uuid.uuid4()

FAKE_LEAD_DATA = {
    "id": LEAD_ID,
    "title": "We're hiring engineers",
    "body": "Growing team at acme.io",
    "source": "reddit",
    "signal_type": "hiring",
    "company_name": None,
    "company_domain": "acme.io",
    "person_name": "founder",
    "keywords": ["python", "fastapi"],
    "posted_at": datetime(2024, 4, 7, tzinfo=UTC),
    "status": "new",
}

FAKE_CLASSIFICATION = {
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

FAKE_ENRICHMENT_RESULT = EnrichmentResult(
    refined_signal_type="hiring",
    refined_signal_strength=75,
    company_stage="startup",
    decision_maker_likelihood=80,
    urgency_score=60,
    icp_fit_score=85,
    extracted_stack=["python", "fastapi"],
    pain_summary="Growing team needs senior engineers",
    recommended_approach="cold_dm",
)


class TestFetchStage:
    """Stage 1: Fetch lead data from repository."""

    @pytest.mark.asyncio
    async def test_fetches_lead(self) -> None:
        repo = AsyncMock()
        repo.get_lead_status.return_value = "new"
        repo.get_lead_by_id.return_value = FAKE_LEAD_DATA
        repo.update_lead_status = AsyncMock()

        stage = FetchStage(repo)
        ctx = await stage.execute(PipelineContext(lead_id=LEAD_ID))

        assert ctx.lead_data is not None
        assert ctx.company_domain == "acme.io"
        repo.update_lead_status.assert_awaited_with(LEAD_ID, "enriching")

    @pytest.mark.asyncio
    async def test_already_processed(self) -> None:
        repo = AsyncMock()
        repo.get_lead_status.return_value = "scored"

        stage = FetchStage(repo)
        with pytest.raises(AlreadyProcessedError):
            await stage.execute(PipelineContext(lead_id=LEAD_ID))

    @pytest.mark.asyncio
    async def test_lead_not_found(self) -> None:
        repo = AsyncMock()
        repo.get_lead_status.return_value = None

        stage = FetchStage(repo)
        with pytest.raises(ValueError, match="not found"):
            await stage.execute(PipelineContext(lead_id=LEAD_ID))


class TestResolveCompanyStage:
    """Stage 2: Resolve company via LLM."""

    @pytest.mark.asyncio
    async def test_skips_when_domain_present(self) -> None:
        resolver = AsyncMock()
        stage = ResolveCompanyStage(resolver)
        ctx = PipelineContext(lead_id=LEAD_ID, company_domain="acme.io")

        result = await stage.execute(ctx)
        assert result.company_domain == "acme.io"
        resolver.resolve.assert_not_awaited()

    @pytest.mark.asyncio
    async def test_resolves_when_missing(self) -> None:
        resolver = AsyncMock()
        resolver.resolve.return_value = ("Acme Inc", "acme.io")
        stage = ResolveCompanyStage(resolver)
        ctx = PipelineContext(
            lead_id=LEAD_ID,
            lead_data={"title": "Test", "body": "Body", "person_name": "user"},
        )

        result = await stage.execute(ctx)
        assert result.company_name == "Acme Inc"
        assert result.company_domain == "acme.io"


class TestEnrichCompanyStage:
    """Stage 3: HTTP-based company enrichment."""

    @pytest.mark.asyncio
    async def test_skips_when_no_domain(self) -> None:
        repo = AsyncMock()
        client = AsyncMock()
        stage = EnrichCompanyStage(repo, client)
        ctx = PipelineContext(lead_id=LEAD_ID)

        result = await stage.execute(ctx)
        assert result.company_enrichment is None

    @pytest.mark.asyncio
    async def test_uses_cache(self) -> None:
        repo = AsyncMock()
        repo.get_cached_company.return_value = {"is_reachable": True, "homepage_title": "Acme"}
        client = AsyncMock()
        stage = EnrichCompanyStage(repo, client)
        ctx = PipelineContext(lead_id=LEAD_ID, company_domain="acme.io")

        result = await stage.execute(ctx)
        assert result.company_enrichment is not None
        assert result.company_enrichment["homepage_title"] == "Acme"


class TestClassifyStage:
    """Stage 4: LLM classification."""

    @pytest.mark.asyncio
    async def test_classifies_lead(self) -> None:
        llm = AsyncMock()
        llm.complete_structured.return_value = FAKE_CLASSIFICATION.copy()
        repo = AsyncMock()
        repo.get_daily_llm_cost.return_value = 0.0
        repo.log_llm_call = AsyncMock()

        from unittest.mock import MagicMock

        from config import Settings

        loader = MagicMock()
        loader.render.return_value = "test prompt"
        settings = Settings()

        stage = ClassifyStage(llm, repo, loader, settings)
        ctx = PipelineContext(lead_id=LEAD_ID, lead_data=FAKE_LEAD_DATA)

        result = await stage.execute(ctx)
        assert result.classification is not None
        assert result.classification.recommended_approach == "cold_dm"
        repo.log_llm_call.assert_awaited_once()

    @pytest.mark.asyncio
    async def test_budget_exceeded(self) -> None:
        llm = AsyncMock()
        repo = AsyncMock()
        repo.get_daily_llm_cost.return_value = 999.0
        repo.update_lead_status = AsyncMock()

        from unittest.mock import MagicMock

        from config import Settings

        loader = MagicMock()
        settings = Settings(daily_llm_budget_usd=10.0)

        stage = ClassifyStage(llm, repo, loader, settings)
        ctx = PipelineContext(lead_id=LEAD_ID, lead_data=FAKE_LEAD_DATA)

        with pytest.raises(BudgetExceededError):
            await stage.execute(ctx)
        repo.update_lead_status.assert_awaited_with(LEAD_ID, "budget_paused")


class TestScoreStage:
    """Stage 5: Pure scoring."""

    @pytest.mark.asyncio
    async def test_scores_lead(self) -> None:
        from config import Settings

        settings = Settings()
        stage = ScoreStage(settings)
        ctx = PipelineContext(
            lead_id=LEAD_ID,
            lead_data=FAKE_LEAD_DATA,
            classification=FAKE_ENRICHMENT_RESULT,
        )

        result = await stage.execute(ctx)
        assert result.final_score is not None
        assert 0 <= result.final_score <= 100

    @pytest.mark.asyncio
    async def test_requires_classification(self) -> None:
        from config import Settings

        stage = ScoreStage(Settings())
        ctx = PipelineContext(lead_id=LEAD_ID)

        with pytest.raises(ValueError, match="classification"):
            await stage.execute(ctx)


class TestPersistStage:
    """Stage 6: Persist and publish event."""

    @pytest.mark.asyncio
    async def test_persists_and_publishes(self) -> None:
        from application.bus import EventBus

        repo = AsyncMock()
        bus = EventBus(max_queue_size=10)
        stage = PersistStage(repo, bus)

        ctx = PipelineContext(
            lead_id=LEAD_ID,
            classification=FAKE_ENRICHMENT_RESULT,
            final_score=78.5,
        )

        await stage.execute(ctx)

        repo.upsert_enrichment.assert_awaited_once()
        repo.update_lead_scores.assert_awaited_once()

        from domain.events import LeadScored
        assert bus.queue_size(LeadScored) == 1
