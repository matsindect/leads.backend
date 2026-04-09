"""Stage 3: Enrich Company — HTTP HEAD + title scrape, cached 30 days."""

from __future__ import annotations

import re
from dataclasses import replace
from datetime import UTC, datetime, timedelta

import httpx
import structlog

from domain.interfaces import EnrichmentRepository
from domain.models import PipelineContext

logger = structlog.get_logger()


class EnrichCompanyStage:
    """Best-effort company enrichment via HTTP checks."""

    def __init__(
        self, repository: EnrichmentRepository, http_client: httpx.AsyncClient
    ) -> None:
        self._repo = repository
        self._client = http_client

    async def execute(self, context: PipelineContext) -> PipelineContext:
        """Enrich company data. Skips gracefully if domain unknown."""
        if not context.company_domain:
            return context

        log = logger.bind(
            lead_id=str(context.lead_id),
            stage="enrich_company",
            domain=context.company_domain,
        )

        # Check cache first
        cached = await self._repo.get_cached_company(context.company_domain)
        if cached:
            log.debug("cache_hit")
            return replace(context, company_enrichment=cached)

        # Fresh enrichment
        enrichment = await self._fetch_company_data(context.company_domain, log)

        # Cache for 30 days
        await self._repo.cache_company(context.company_domain, enrichment)

        return replace(context, company_enrichment=enrichment)

    async def _fetch_company_data(
        self, domain: str, log: structlog.stdlib.BoundLogger  # type: ignore[type-arg]
    ) -> dict:
        """HTTP HEAD to check reachability, then GET for title."""
        data: dict = {
            "is_reachable": False,
            "homepage_title": None,
            "enriched_at": datetime.now(UTC),
            "expires_at": datetime.now(UTC) + timedelta(days=30),
        }

        try:
            resp = await self._client.head(
                f"https://{domain}", timeout=10.0, follow_redirects=True
            )
            data["is_reachable"] = resp.status_code < 400
        except (httpx.TransportError, httpx.HTTPStatusError):
            log.debug("head_failed")
            return data

        if data["is_reachable"]:
            try:
                page = await self._client.get(
                    f"https://{domain}", timeout=10.0, follow_redirects=True
                )
                match = re.search(r"<title[^>]*>(.*?)</title>", page.text, re.I | re.S)
                if match:
                    data["homepage_title"] = match.group(1).strip()[:200]
            except (httpx.TransportError, httpx.HTTPStatusError):
                log.debug("title_scrape_failed")

        log.info("enriched", reachable=data["is_reachable"], title=data.get("homepage_title"))
        return data
