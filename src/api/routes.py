"""FastAPI route definitions.

All business logic is delegated to injected collaborators — routes are thin
translation layers between HTTP and domain objects.
"""

from __future__ import annotations

from datetime import datetime
from decimal import Decimal
from typing import Any
from uuid import UUID

from fastapi import APIRouter, HTTPException, Request
from pydantic import BaseModel

from api.dependencies import (
    AdaptersDep,
    EnrichmentRepoDep,
    EventBusDep,
    OrchestratorDep,
    PipelineDep,
    PublisherDep,
    RepositoryDep,
    SettingsDep,
)
from api.schemas import AdapterParamSchema, ScrapeRequest
from domain.events import LeadCreated
from domain.models import AlreadyProcessedError
from modules.enrichment.stages.classify import BudgetExceededError

# TODO: Add auth middleware here when moving beyond internal network deployment.

router = APIRouter()


# ---------------------------------------------------------------------------
# Scraping endpoints (existing, unchanged)
# ---------------------------------------------------------------------------


@router.post("/scrape/{adapter_name}")
async def trigger_scrape(
    adapter_name: str,
    orchestrator: OrchestratorDep,
    adapters: AdaptersDep,
    request: ScrapeRequest | None = None,
) -> dict[str, Any]:
    """Trigger a single scrape pass for one adapter. Idempotent.

    Body is optional — an empty body uses env-configured defaults.
    Any field in ``ScrapeRequest`` overrides the corresponding env setting.
    """
    adapter = adapters.get(adapter_name)
    if adapter is None:
        raise HTTPException(
            status_code=404,
            detail=f"Unknown adapter: {adapter_name}. Available: {list(adapters.keys())}",
        )

    report = await orchestrator.run(adapter, request or ScrapeRequest())

    return {
        "adapter": report.adapter_name,
        "run_id": str(report.run_id),
        "fetched": report.fetched,
        "normalized": report.normalized,
        "inserted": report.inserted,
        "duplicates": report.duplicates,
        "errors": report.errors,
        "duration_ms": report.duration_ms,
        "error": report.error,
    }


@router.get("/adapters/{adapter_name}/schema")
async def adapter_schema(
    adapter_name: str,
    adapters: AdaptersDep,
) -> AdapterParamSchema:
    """Describe which ScrapeRequest fields the named adapter uses.

    Lets n8n workflow authors discover which params to supply.
    """
    adapter = adapters.get(adapter_name)
    if adapter is None:
        raise HTTPException(
            status_code=404,
            detail=f"Unknown adapter: {adapter_name}. Available: {list(adapters.keys())}",
        )
    return adapter.accepted_params


@router.get("/adapters")
async def list_adapters(
    adapters: AdaptersDep,
    repository: RepositoryDep,
) -> list[dict[str, Any]]:
    """List all registered adapters with their config and last-run info."""
    adapter_names = list(adapters.keys())
    infos = await repository.get_all_adapter_info(adapter_names)

    result: list[dict[str, Any]] = []
    for info in infos:
        adapter = adapters.get(info.name)
        result.append({
            "name": info.name,
            "poll_interval_seconds": adapter.poll_interval_seconds if adapter else 0,
            "last_run_at": info.last_run_at.isoformat() if info.last_run_at else None,
            "last_status": info.last_status,
        })
    return result


@router.get("/health")
async def health_check(
    repository: RepositoryDep,
    publisher: PublisherDep,
) -> dict[str, Any]:
    """Liveness probe — returns DB + event bus connectivity status."""
    db_ok = await repository.check_connectivity()
    bus_ok = await publisher.check_connectivity()

    return {
        "status": "healthy" if (db_ok and bus_ok) else "degraded",
        "database": "connected" if db_ok else "unreachable",
        "event_bus": "connected" if bus_ok else "unreachable",
    }


@router.get("/health/scrapers")
async def scraper_health(
    adapters: AdaptersDep,
    repository: RepositoryDep,
) -> list[dict[str, Any]]:
    """Per-adapter health: last success, last error, records in 24h."""
    adapter_names = list(adapters.keys())
    healths = await repository.get_all_adapter_health(adapter_names)

    return [
        {
            "name": h.name,
            "last_success_at": h.last_success_at.isoformat() if h.last_success_at else None,
            "last_error": h.last_error,
            "records_last_24h": h.records_last_24h,
            "circuit_open": h.circuit_open,
        }
        for h in healths
    ]


# ---------------------------------------------------------------------------
# Enrichment endpoints (new)
# ---------------------------------------------------------------------------


@router.post("/enrich/{lead_id}")
async def enrich_lead(
    lead_id: UUID,
    pipeline: PipelineDep,
    settings: SettingsDep,
) -> dict[str, Any]:
    """Manually trigger enrichment for a specific lead. Idempotent."""
    if not settings.enable_enrichment or pipeline is None:
        raise HTTPException(
            status_code=503,
            detail="Enrichment is disabled. Set LEADS_ENABLE_ENRICHMENT=true.",
        )

    try:
        context = await pipeline.execute(lead_id)
    except AlreadyProcessedError:
        return {"status": "already_processed", "lead_id": str(lead_id)}
    except BudgetExceededError:
        return {"status": "budget_paused", "lead_id": str(lead_id)}

    return {
        "status": "enriched",
        "lead_id": str(lead_id),
        "score": context.final_score,
        "recommended_approach": (
            context.classification.recommended_approach
            if context.classification else None
        ),
    }


class ReprocessRequest(BaseModel):
    """Body for POST /reprocess."""

    status: str = "scored"
    since: str = "2026-01-01"


@router.post("/reprocess")
async def reprocess_leads(
    body: ReprocessRequest,
    repository: EnrichmentRepoDep,
    bus: EventBusDep,
    settings: SettingsDep,
) -> dict[str, Any]:
    """Republish LeadCreated events for matching leads.

    Useful when scoring formula or prompts change.
    """
    if not settings.enable_enrichment:
        raise HTTPException(
            status_code=503,
            detail="Enrichment is disabled. Set LEADS_ENABLE_ENRICHMENT=true.",
        )
    leads = await repository.get_pending_leads(
        statuses=[body.status],
        older_than_minutes=0,
        limit=500,
    )

    for lead in leads:
        await bus.publish(
            LeadCreated(
                lead_id=lead["id"],
                source=lead.get("source", "unknown"),
                signal_type=lead.get("signal_type"),
            )
        )

    return {"requeued": len(leads), "status_filter": body.status}


@router.get("/stats/cost")
async def llm_cost_stats(
    repository: EnrichmentRepoDep,
) -> dict[str, Any]:
    """LLM cost aggregation per day / stage / model."""
    aggregation = await repository.get_cost_aggregation()
    daily_total = await repository.get_daily_llm_cost()

    return {
        "today_total_usd": daily_total,
        "breakdown": [
            {
                "day": str(row["day"]) if row.get("day") else None,
                "stage": row["stage"],
                "model": row["model"],
                "total_cost_usd": float(row["total_cost"]),
                "total_input_tokens": row["total_input_tokens"],
                "total_output_tokens": row["total_output_tokens"],
                "call_count": row["call_count"],
            }
            for row in aggregation
        ],
    }


# ---------------------------------------------------------------------------
# Leads read API (for frontend consumption)
# ---------------------------------------------------------------------------


@router.get("/leads")
async def list_leads(
    repository: EnrichmentRepoDep,
    source: str | None = None,
    signal_type: str | None = None,
    status: str | None = None,
    search: str | None = None,
    sort_by: str = "fetched_at",
    sort_order: str = "desc",
    page: int = 1,
    page_size: int = 50,
) -> dict[str, Any]:
    """List leads with filtering, sorting, and pagination.

    Query params:
    - source: filter by source (reddit, hackernews, etc.)
    - signal_type: filter by signal (hiring, pain_point, etc.)
    - status: filter by status (new, scored, etc.)
    - search: text search across title, body, company, person
    - sort_by: field to sort (fetched_at, score, signal_strength, posted_at)
    - sort_order: asc or desc
    - page: page number (1-based)
    - page_size: items per page (max 100)
    """
    page_size = min(page_size, 100)
    offset = (max(page, 1) - 1) * page_size

    rows, total = await repository.query_leads(
        source=source,
        signal_type=signal_type,
        status=status,
        search=search,
        sort_by=sort_by,
        sort_order=sort_order,
        offset=offset,
        limit=page_size,
    )

    return {
        "leads": [_serialize_lead(r) for r in rows],
        "total": total,
        "page": page,
        "page_size": page_size,
        "total_pages": (total + page_size - 1) // page_size if total else 0,
    }


@router.get("/leads/stats")
async def lead_stats(
    repository: EnrichmentRepoDep,
) -> dict[str, Any]:
    """Dashboard stats: totals, per-source, per-signal, per-status."""
    return await repository.get_lead_stats()


@router.get("/leads/{lead_id}")
async def get_lead(
    lead_id: UUID,
    repository: EnrichmentRepoDep,
) -> dict[str, Any]:
    """Get a single lead with enrichment data."""
    lead = await repository.get_lead_detail(lead_id)
    if lead is None:
        raise HTTPException(status_code=404, detail="Lead not found")
    return _serialize_lead(lead)


def _serialize_lead(row: dict[str, Any]) -> dict[str, Any]:
    """Convert a DB row to a JSON-safe dict for the API response."""
    result: dict[str, Any] = {}
    for key, val in row.items():
        if isinstance(val, datetime):
            result[key] = val.isoformat()
        elif isinstance(val, UUID):
            result[key] = str(val)
        elif isinstance(val, Decimal):
            result[key] = float(val)
        else:
            result[key] = val
    return result


@router.get("/health/enrichment")
async def enrichment_health(
    request: Request,
    repository: EnrichmentRepoDep,
    settings: SettingsDep,
) -> dict[str, Any]:
    """Enrichment worker health: events, circuit breaker, LLM spend."""
    worker = getattr(request.app.state, "enrichment_worker", None)
    worker_stats = worker.stats if worker else {"status": "not_running"}

    daily_cost = await repository.get_daily_llm_cost()

    return {
        "worker": worker_stats,
        "llm_spend_today_usd": daily_cost,
        "llm_budget_usd": settings.daily_llm_budget_usd,
        "budget_remaining_usd": max(0, settings.daily_llm_budget_usd - daily_cost),
    }
