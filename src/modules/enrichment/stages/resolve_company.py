"""Stage 2: Resolve Company — extract company name/domain via cheap LLM call."""

from __future__ import annotations

from dataclasses import replace

import structlog

from domain.interfaces import CompanyResolver
from domain.models import PipelineContext

logger = structlog.get_logger()


class ResolveCompanyStage:
    """Use LLM to extract company info when company_domain is missing."""

    def __init__(self, resolver: CompanyResolver) -> None:
        self._resolver = resolver

    async def execute(self, context: PipelineContext) -> PipelineContext:
        """Skip if company_domain already known. Otherwise call resolver."""
        if context.company_domain:
            return context

        log = logger.bind(lead_id=str(context.lead_id), stage="resolve_company")
        lead = context.lead_data or {}

        company_name, company_domain = await self._resolver.resolve(
            title=lead.get("title", ""),
            body=lead.get("body", ""),
            person_name=lead.get("person_name"),
        )

        log.info("resolved", company_name=company_name, company_domain=company_domain)

        return replace(
            context,
            company_name=company_name or context.company_name,
            company_domain=company_domain or context.company_domain,
        )
