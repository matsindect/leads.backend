"""Stage 1: Fetch — load lead from Postgres, check idempotency."""

from __future__ import annotations

from dataclasses import replace

import structlog

from domain.interfaces import EnrichmentRepository
from domain.models import AlreadyProcessedError, PipelineContext

logger = structlog.get_logger()

# Leads in these statuses have already completed the pipeline
_TERMINAL_STATUSES = frozenset({"scored", "sent", "closed", "dead"})


class FetchStage:
    """Load lead data and guard against re-processing."""

    def __init__(self, repository: EnrichmentRepository) -> None:
        self._repo = repository

    async def execute(self, context: PipelineContext) -> PipelineContext:
        """Fetch lead row from DB. Raise AlreadyProcessedError if terminal."""
        log = logger.bind(lead_id=str(context.lead_id), stage="fetch")

        status = await self._repo.get_lead_status(context.lead_id)
        if status is None:
            raise ValueError(f"Lead {context.lead_id} not found")

        if status in _TERMINAL_STATUSES:
            log.info("already_processed", status=status)
            raise AlreadyProcessedError(f"Lead {context.lead_id} already in status={status}")

        # Mark as enriching to claim the work
        await self._repo.update_lead_status(context.lead_id, "enriching")

        lead_data = await self._repo.get_lead_by_id(context.lead_id)
        log.info("lead_fetched", status=status)

        return replace(
            context,
            lead_data=lead_data,
            company_name=lead_data.get("company_name") if lead_data else None,
            company_domain=lead_data.get("company_domain") if lead_data else None,
        )
