"""Domain events exchanged between modules via the in-process EventBus.

Events are immutable dataclasses.  The bus routes by event type —
subscribers register for a specific class and receive only those events.
"""

from __future__ import annotations

from dataclasses import dataclass
from uuid import UUID


@dataclass(frozen=True, slots=True)
class LeadCreated:
    """Published by the scraping module after a new lead is inserted.

    The enrichment worker subscribes to this event to trigger the
    enrichment pipeline.
    """

    lead_id: UUID
    source: str
    signal_type: str | None


@dataclass(frozen=True, slots=True)
class LeadScored:
    """Published by the enrichment module after a lead is fully scored.

    Downstream consumers (e.g. future outreach module) subscribe to this.
    """

    lead_id: UUID
    score: float
    recommended_approach: str
