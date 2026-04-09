"""Stage 5: Score — pure scoring computation, delegates to scoring.py."""

from __future__ import annotations

from dataclasses import replace
from datetime import datetime, timezone

import structlog

from config import Settings
from domain.models import PipelineContext
from modules.enrichment.scoring import compute_final_score

logger = structlog.get_logger()


class ScoreStage:
    """Apply the pure scoring function to the classification result."""

    def __init__(self, settings: Settings) -> None:
        self._settings = settings

    async def execute(self, context: PipelineContext) -> PipelineContext:
        """Compute final score from classification + metadata."""
        if context.classification is None:
            raise ValueError("ScoreStage requires classification to be set")

        lead = context.lead_data or {}
        posted_at = lead.get("posted_at")

        score = compute_final_score(
            result=context.classification,
            posted_at=posted_at,
            user_skills=self._settings.user_skills,
        )

        logger.info(
            "scored",
            lead_id=str(context.lead_id),
            stage="score",
            final_score=score,
        )

        return replace(context, final_score=score)
