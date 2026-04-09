"""Protocol definitions that decouple domain logic from infrastructure.

Concrete implementations live in ``infrastructure/`` and ``modules/``.
The orchestrator and API layers depend only on these protocols.
"""

from __future__ import annotations

import enum
from collections.abc import Sequence
from datetime import date, datetime
from typing import Any, Protocol
from uuid import UUID

from domain.models import (
    AdapterHealth,
    AdapterInfo,
    CanonicalLead,
    RunReport,
)

# ---------------------------------------------------------------------------
# Scraping protocols (existing)
# ---------------------------------------------------------------------------


class SourceAdapter(Protocol):
    """Contract every external-source adapter must satisfy."""

    @property
    def name(self) -> str:
        """Unique identifier used in routes and logging."""
        ...

    @property
    def poll_interval_seconds(self) -> int:
        """Suggested interval between automatic scrape passes."""
        ...

    async def fetch_raw(self) -> list[dict]:
        """Pull raw records from the external source.

        May raise on transient failures — the orchestrator handles retries.
        """
        ...

    def normalize(self, raw: dict) -> CanonicalLead | None:
        """Convert one raw record into a CanonicalLead.

        Returns None if the record should be dropped (e.g. no signal match).
        Must be a pure function — no I/O, no side effects.
        """
        ...


class LeadRepository(Protocol):
    """Persistence layer for leads and scrape run history."""

    async def insert_leads(
        self, leads: Sequence[CanonicalLead]
    ) -> tuple[list[UUID], int]:
        """Insert leads, skipping duplicates via ON CONFLICT.

        Returns (list of inserted UUIDs, count of duplicates skipped).
        """
        ...

    async def record_run(self, report: RunReport) -> None:
        """Persist a scrape run report."""
        ...

    async def get_adapter_info(self, adapter_name: str) -> AdapterInfo | None:
        """Fetch last-run metadata for an adapter."""
        ...

    async def get_all_adapter_info(
        self, adapter_names: Sequence[str]
    ) -> list[AdapterInfo]:
        """Fetch metadata for all known adapters."""
        ...

    async def get_adapter_health(self, adapter_name: str) -> AdapterHealth:
        """Detailed health for one adapter."""
        ...

    async def get_all_adapter_health(
        self, adapter_names: Sequence[str]
    ) -> list[AdapterHealth]:
        """Detailed health for all adapters."""
        ...

    async def count_recent_failures(
        self, adapter_name: str, limit: int, within_seconds: int | None = None
    ) -> int:
        """Count consecutive recent failures for circuit-breaker logic.

        If ``within_seconds`` is given, only failures newer than that
        cutoff count — older errors are ignored, allowing the breaker
        to auto-reset after the cooldown period.
        """
        ...

    async def check_connectivity(self) -> bool:
        """Return True if the database is reachable."""
        ...


class EventPublisher(Protocol):
    """Publishes domain events via the in-process EventBus."""

    async def publish_new_leads(
        self, lead_ids: list[UUID], source: str, signal_type: str | None
    ) -> None:
        """Emit one LeadCreated event per inserted lead."""
        ...

    async def check_connectivity(self) -> bool:
        """Return True if the event bus is reachable."""
        ...


# ---------------------------------------------------------------------------
# Enrichment protocols (new)
# ---------------------------------------------------------------------------


class ModelHint(enum.StrEnum):
    """Guides the LLM provider to choose an appropriate model tier."""

    CHEAP = "cheap"
    SMART = "smart"


class LLMProvider(Protocol):
    """Abstraction over LLM APIs for structured completions."""

    async def complete_structured(
        self, prompt: str, schema: dict[str, Any], model_hint: ModelHint
    ) -> dict[str, Any]:
        """Send a prompt and get back JSON conforming to *schema*.

        The provider maps *model_hint* to a concrete model ID via config.
        Returns the parsed JSON dict.
        """
        ...


class EnrichmentRepository(Protocol):
    """Persistence layer for enrichment-specific data."""

    async def get_lead_by_id(self, lead_id: UUID) -> dict[str, Any] | None:
        """Fetch a raw_leads row by primary key."""
        ...

    async def get_lead_status(self, lead_id: UUID) -> str | None:
        """Return the current status of a lead, or None if not found."""
        ...

    async def update_lead_status(self, lead_id: UUID, status: str) -> None:
        """Update the status column on raw_leads."""
        ...

    async def upsert_enrichment(self, lead_id: UUID, data: dict[str, Any]) -> None:
        """Insert or update a row in lead_enrichments."""
        ...

    async def update_lead_scores(
        self,
        lead_id: UUID,
        *,
        score: float,
        enriched_at: datetime,
        scored_at: datetime,
    ) -> None:
        """Update raw_leads with enrichment timestamps and final score."""
        ...

    async def get_cached_company(self, domain: str) -> dict[str, Any] | None:
        """Fetch a company_enrichments row if not expired."""
        ...

    async def cache_company(self, domain: str, data: dict[str, Any]) -> None:
        """Upsert a company_enrichments row with expiry."""
        ...

    async def get_cached_resolution(self, cache_key: str) -> dict[str, Any] | None:
        """Fetch a company_resolutions row."""
        ...

    async def cache_resolution(
        self, cache_key: str, company_name: str | None, company_domain: str | None
    ) -> None:
        """Insert a company_resolutions cache entry."""
        ...

    async def log_llm_call(
        self,
        *,
        lead_id: UUID | None,
        stage: str,
        model: str,
        input_tokens: int,
        output_tokens: int,
        cost_usd: float,
    ) -> None:
        """Append a row to llm_call_log for cost tracking."""
        ...

    async def get_daily_llm_cost(self, day: date | None = None) -> float:
        """Sum cost_usd for a given day (default: today)."""
        ...

    async def get_cost_aggregation(self) -> list[dict[str, Any]]:
        """Return cost aggregated by day, stage, model."""
        ...

    async def get_pending_leads(
        self, statuses: Sequence[str], older_than_minutes: int, limit: int
    ) -> list[dict[str, Any]]:
        """Find leads stuck in intermediate statuses for resweep."""
        ...


class CompanyResolver(Protocol):
    """Resolves company name/domain from lead text using an LLM."""

    async def resolve(
        self, title: str, body: str, person_name: str | None
    ) -> tuple[str | None, str | None]:
        """Return (company_name, company_domain) or (None, None)."""
        ...
