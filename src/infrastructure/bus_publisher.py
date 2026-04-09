"""EventPublisher backed by the in-process EventBus.

Replaces RedisEventPublisher.  Satisfies ``domain.interfaces.EventPublisher``.
"""

from __future__ import annotations

from uuid import UUID

import structlog

from application.bus import EventBus
from domain.events import LeadCreated

logger = structlog.get_logger()


class BusEventPublisher:
    """Publishes LeadCreated events to the in-process EventBus."""

    def __init__(self, bus: EventBus) -> None:
        self._bus = bus

    async def publish_new_leads(
        self, lead_ids: list[UUID], source: str, signal_type: str | None
    ) -> None:
        """Emit one LeadCreated event per inserted lead."""
        for lead_id in lead_ids:
            event = LeadCreated(
                lead_id=lead_id,
                source=source,
                signal_type=signal_type,
            )
            await self._bus.publish(event)
            logger.debug(
                "lead_created_event_published",
                lead_id=str(lead_id),
                source=source,
            )

    async def check_connectivity(self) -> bool:
        """In-process bus is always available."""
        return True
