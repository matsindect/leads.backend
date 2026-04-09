"""Stage 4: Classify — LLM-powered lead classification with structured output."""

from __future__ import annotations

from dataclasses import replace

import structlog
from pydantic import BaseModel, Field

from config import Settings
from domain.interfaces import EnrichmentRepository, LLMProvider, ModelHint
from domain.models import EnrichmentResult, PipelineContext
from infrastructure.prompt_loader import PromptLoader

logger = structlog.get_logger()

# Cost per token (approximate, updated as pricing changes)
_COST_PER_INPUT_TOKEN = 0.000003
_COST_PER_OUTPUT_TOKEN = 0.000015


class ClassificationResponse(BaseModel):
    """Pydantic validation model for LLM classification output."""

    refined_signal_type: str
    refined_signal_strength: int = Field(ge=0, le=100)
    company_stage: str | None
    decision_maker_likelihood: int = Field(ge=0, le=100)
    urgency_score: int = Field(ge=0, le=100)
    icp_fit_score: int = Field(ge=0, le=100)
    extracted_stack: list[str]
    pain_summary: str
    recommended_approach: str
    skip_reason: str | None = None


class ClassifyStage:
    """Send lead to LLM for structured classification."""

    def __init__(
        self,
        llm: LLMProvider,
        repository: EnrichmentRepository,
        prompt_loader: PromptLoader,
        settings: Settings,
    ) -> None:
        self._llm = llm
        self._repo = repository
        self._prompt_loader = prompt_loader
        self._settings = settings

    async def execute(self, context: PipelineContext) -> PipelineContext:
        """Call SMART model for classification. Validate with Pydantic."""
        log = logger.bind(lead_id=str(context.lead_id), stage="classify")
        lead = context.lead_data or {}

        # Cost ceiling check
        daily_cost = await self._repo.get_daily_llm_cost()
        if daily_cost >= self._settings.daily_llm_budget_usd:
            log.warning("budget_exceeded", daily_cost=daily_cost)
            await self._repo.update_lead_status(context.lead_id, "budget_paused")
            raise BudgetExceededError(
                f"Daily LLM budget ${self._settings.daily_llm_budget_usd} exceeded"
            )

        prompt = self._prompt_loader.render(
            "lead_classification.jinja2",
            title=lead.get("title", ""),
            body=lead.get("body", ""),
            source=lead.get("source", ""),
            signal_type=lead.get("signal_type", "unknown"),
            company_name=context.company_name,
            company_domain=context.company_domain,
            company_enrichment=context.company_enrichment,
            stack_mentions=lead.get("stack_mentions", []),
            user_skills=self._settings.user_skills,
        )

        raw_result = await self._llm.complete_structured(
            prompt=prompt,
            schema=ClassificationResponse.model_json_schema(),
            model_hint=ModelHint.SMART,
        )

        # Extract usage metadata before validation
        usage = raw_result.pop("_usage", {})

        # Validate response
        validated = ClassificationResponse.model_validate(raw_result)

        # Log LLM call for cost tracking
        input_tokens = usage.get("input_tokens", 0)
        output_tokens = usage.get("output_tokens", 0)
        model = usage.get("model", "unknown")
        cost = input_tokens * _COST_PER_INPUT_TOKEN + output_tokens * _COST_PER_OUTPUT_TOKEN

        await self._repo.log_llm_call(
            lead_id=context.lead_id,
            stage="classify",
            model=model,
            input_tokens=input_tokens,
            output_tokens=output_tokens,
            cost_usd=cost,
        )

        log.info(
            "classified",
            approach=validated.recommended_approach,
            strength=validated.refined_signal_strength,
        )

        enrichment = EnrichmentResult(
            refined_signal_type=validated.refined_signal_type,
            refined_signal_strength=validated.refined_signal_strength,
            company_stage=validated.company_stage,
            decision_maker_likelihood=validated.decision_maker_likelihood,
            urgency_score=validated.urgency_score,
            icp_fit_score=validated.icp_fit_score,
            extracted_stack=validated.extracted_stack,
            pain_summary=validated.pain_summary,
            recommended_approach=validated.recommended_approach,
            skip_reason=validated.skip_reason,
        )

        return replace(context, classification=enrichment)


class BudgetExceededError(Exception):
    """Raised when the daily LLM budget is exceeded."""
