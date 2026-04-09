"""Stage 6: Persist — save enrichment results and publish LeadScored event."""

from __future__ import annotations

from datetime import UTC, datetime

import structlog

from application.bus import EventBus
from domain.events import LeadScored
from domain.interfaces import EnrichmentRepository
from domain.models import PipelineContext

logger = structlog.get_logger()


class PersistStage:
    """Write enrichment results to Postgres and emit LeadScored."""

    def __init__(self, repository: EnrichmentRepository, event_bus: EventBus) -> None:
        self._repo = repository
        self._bus = event_bus

    async def execute(self, context: PipelineContext) -> PipelineContext:
        """Persist enrichment + scores, then publish event."""
        if context.classification is None or context.final_score is None:
            raise ValueError("PersistStage requires classification and final_score")

        log = logger.bind(lead_id=str(context.lead_id), stage="persist")
        now = datetime.now(UTC)
        result = context.classification

        # Upsert enrichment row (ON CONFLICT DO UPDATE for idempotency)
        await self._repo.upsert_enrichment(
            context.lead_id,
            {
                "refined_signal_type": result.refined_signal_type,
                "refined_signal_strength": result.refined_signal_strength,
                "company_stage": result.company_stage,
                "decision_maker_likelihood": result.decision_maker_likelihood,
                "urgency_score": result.urgency_score,
                "icp_fit_score": result.icp_fit_score,
                "extracted_stack": result.extracted_stack,
                "pain_summary": result.pain_summary,
                "recommended_approach": result.recommended_approach,
                "skip_reason": result.skip_reason,
                "enriched_at": now,
            },
        )

        # Update raw_leads with scores
        await self._repo.update_lead_scores(
            context.lead_id,
            score=context.final_score,
            enriched_at=now,
            scored_at=now,
        )

        log.info("persisted", score=context.final_score)

        # Publish LeadScored event
        await self._bus.publish(
            LeadScored(
                lead_id=context.lead_id,
                score=context.final_score,
                recommended_approach=result.recommended_approach,
            )
        )

        return context
