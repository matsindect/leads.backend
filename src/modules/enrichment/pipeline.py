"""Enrichment pipeline — composes stages into a single execute() call.

Each stage receives a PipelineContext and returns a new (immutable) context
with additional fields populated.  Adding a new stage requires only:
1. A new file in stages/
2. One line in the stage list below
"""

from __future__ import annotations

from uuid import UUID

import httpx
import structlog

from application.bus import EventBus
from config import Settings
from domain.interfaces import CompanyResolver, EnrichmentRepository, LLMProvider
from domain.models import AlreadyProcessed, PipelineContext
from infrastructure.prompt_loader import PromptLoader
from modules.enrichment.stages.classify import BudgetExceeded, ClassifyStage
from modules.enrichment.stages.enrich_company import EnrichCompanyStage
from modules.enrichment.stages.fetch import FetchStage
from modules.enrichment.stages.persist import PersistStage
from modules.enrichment.stages.resolve_company import ResolveCompanyStage
from modules.enrichment.stages.score import ScoreStage

logger = structlog.get_logger()


class EnrichmentPipeline:
    """Runs the 6-stage enrichment pipeline for a single lead.

    Dependencies are injected via constructor — the pipeline is
    constructible with mocks for testing.
    """

    def __init__(
        self,
        repository: EnrichmentRepository,
        llm: LLMProvider,
        resolver: CompanyResolver,
        prompt_loader: PromptLoader,
        http_client: httpx.AsyncClient,
        event_bus: EventBus,
        settings: Settings,
    ) -> None:
        # Build stages with their specific dependencies
        self._stages = [
            FetchStage(repository),
            ResolveCompanyStage(resolver),
            EnrichCompanyStage(repository, http_client),
            ClassifyStage(llm, repository, prompt_loader, settings),
            ScoreStage(settings),
            PersistStage(repository, event_bus),
        ]

    async def execute(self, lead_id: UUID) -> PipelineContext:
        """Run all stages sequentially. Returns the final context.

        Raises AlreadyProcessed for idempotent skips.
        Raises BudgetExceeded when daily LLM limit is hit.
        Other exceptions bubble up for the caller to handle.
        """
        context = PipelineContext(lead_id=lead_id)
        log = logger.bind(lead_id=str(lead_id), module="enrichment")

        for stage in self._stages:
            stage_name = type(stage).__name__
            log.debug("stage_start", stage=stage_name)
            context = await stage.execute(context)
            log.debug("stage_complete", stage=stage_name)

        log.info("pipeline_complete", score=context.final_score)
        return context
