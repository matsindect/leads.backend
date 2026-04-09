"""Company resolver — uses cheap LLM call to extract company info.

Satisfies ``domain.interfaces.CompanyResolver``.
Results are cached in the company_resolutions table.
"""

from __future__ import annotations

import hashlib

import structlog

from domain.interfaces import EnrichmentRepository, LLMProvider, ModelHint
from infrastructure.prompt_loader import PromptLoader

logger = structlog.get_logger()

_COST_PER_INPUT_TOKEN = 0.00000025
_COST_PER_OUTPUT_TOKEN = 0.00000125


class LLMCompanyResolver:
    """Resolves company name/domain from lead text using a cheap LLM call."""

    def __init__(
        self,
        llm: LLMProvider,
        repository: EnrichmentRepository,
        prompt_loader: PromptLoader,
    ) -> None:
        self._llm = llm
        self._repo = repository
        self._prompt_loader = prompt_loader

    async def resolve(
        self, title: str, body: str, person_name: str | None
    ) -> tuple[str | None, str | None]:
        """Return (company_name, company_domain) or (None, None)."""
        cache_key = self._cache_key(title, body)

        # Check cache
        cached = await self._repo.get_cached_resolution(cache_key)
        if cached:
            return cached.get("company_name"), cached.get("company_domain")

        prompt = self._prompt_loader.render(
            "company_resolution.jinja2",
            title=title,
            body=body,
            person_name=person_name,
        )

        try:
            result = await self._llm.complete_structured(
                prompt=prompt,
                schema={"type": "object", "properties": {"company_name": {"type": "string"}, "company_domain": {"type": "string"}}},
                model_hint=ModelHint.CHEAP,
            )

            usage = result.pop("_usage", {})
            input_tokens = usage.get("input_tokens", 0)
            output_tokens = usage.get("output_tokens", 0)
            cost = input_tokens * _COST_PER_INPUT_TOKEN + output_tokens * _COST_PER_OUTPUT_TOKEN

            await self._repo.log_llm_call(
                lead_id=None,
                stage="resolve_company",
                model=usage.get("model", "unknown"),
                input_tokens=input_tokens,
                output_tokens=output_tokens,
                cost_usd=cost,
            )

            company_name = result.get("company_name")
            company_domain = result.get("company_domain")

            # Normalize null-like values
            if company_name in (None, "null", ""):
                company_name = None
            if company_domain in (None, "null", ""):
                company_domain = None

            await self._repo.cache_resolution(cache_key, company_name, company_domain)
            return company_name, company_domain

        except Exception:
            logger.warning("company_resolution_failed", exc_info=True)
            return None, None

    @staticmethod
    def _cache_key(title: str, body: str) -> str:
        content = f"{title}:{body[:500]}"
        return hashlib.sha256(content.encode()).hexdigest()[:32]
