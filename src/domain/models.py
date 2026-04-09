"""Canonical domain models shared across all layers.

These dataclasses define the single source of truth for lead data and run
reporting.  Downstream layers (persistence, events, API) convert to/from
these types — they never invent their own schemas.
"""

from __future__ import annotations

import enum
import uuid
from dataclasses import dataclass, field
from datetime import datetime
from typing import Any


class SignalType(str, enum.Enum):
    """Classification of the buying / interest signal detected."""

    HIRING = "hiring"
    PAIN_POINT = "pain_point"
    TOOL_EVALUATION = "tool_evaluation"
    BUDGET_MENTION = "budget_mention"
    EXPANSION = "expansion"
    TECH_STACK_CHANGE = "tech_stack_change"
    COMPLIANCE_NEED = "compliance_need"
    FUNDING = "funding"
    GENERAL_INTEREST = "general_interest"


@dataclass(frozen=True, slots=True)
class CanonicalLead:
    """Normalized lead produced by every source adapter.

    Immutable so normalize() methods remain pure.
    """

    source: str
    source_id: str
    url: str
    title: str
    body: str
    raw_payload: dict[str, Any]
    signal_type: SignalType | None = None
    signal_strength: int | None = None
    company_name: str | None = None
    company_domain: str | None = None
    person_name: str | None = None
    person_role: str | None = None
    location: str | None = None
    stack_mentions: list[str] = field(default_factory=list)
    posted_at: datetime | None = None


@dataclass(frozen=True, slots=True)
class RunReport:
    """Summary returned after a single scrape pass."""

    adapter_name: str
    run_id: uuid.UUID
    fetched: int
    normalized: int
    inserted: int
    duplicates: int
    errors: int
    duration_ms: int
    started_at: datetime
    error: str | None = None


@dataclass(frozen=True, slots=True)
class AdapterInfo:
    """Metadata about a registered adapter shown by GET /adapters."""

    name: str
    poll_interval_seconds: int
    last_run_at: datetime | None
    last_status: str | None


@dataclass(frozen=True, slots=True)
class AdapterHealth:
    """Per-adapter health details shown by GET /health/scrapers."""

    name: str
    last_success_at: datetime | None
    last_error: str | None
    records_last_24h: int
    circuit_open: bool


# ---------------------------------------------------------------------------
# Enrichment pipeline models
# ---------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class PipelineContext:
    """Immutable context threaded through enrichment pipeline stages.

    Stages return new contexts with additional fields populated —
    they never mutate.
    """

    lead_id: uuid.UUID
    lead_data: dict[str, Any] | None = None
    company_name: str | None = None
    company_domain: str | None = None
    company_enrichment: dict[str, Any] | None = None
    classification: EnrichmentResult | None = None
    final_score: float | None = None


@dataclass(frozen=True, slots=True)
class EnrichmentResult:
    """Structured output from the LLM classification stage."""

    refined_signal_type: str
    refined_signal_strength: int
    company_stage: str | None
    decision_maker_likelihood: int
    urgency_score: int
    icp_fit_score: int
    extracted_stack: list[str]
    pain_summary: str
    recommended_approach: str
    skip_reason: str | None = None


class AlreadyProcessed(Exception):
    """Raised when a lead has already been enriched (idempotency guard)."""
