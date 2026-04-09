"""Background workers launched during FastAPI lifespan.

EnrichmentWorker — subscribes to LeadCreated events from the bus.
PendingLeadsResweeper — periodic safety net that recovers stuck leads.

The durability pattern: Postgres status column is the real queue.
The event bus is just the fast path. The resweeper handles crashes,
queue overflows, and missed events.
"""

from __future__ import annotations

import asyncio
from typing import TYPE_CHECKING

import structlog

from application.bus import EventBus
from domain.events import LeadCreated
from domain.models import AlreadyProcessed
from modules.enrichment.stages.classify import BudgetExceeded

if TYPE_CHECKING:
    from fastapi import FastAPI

    from modules.enrichment.pipeline import EnrichmentPipeline

logger = structlog.get_logger()

# Statuses the resweeper scans for
_RESWEEP_STATUSES = ("new", "pending_enrichment", "budget_paused")


class EnrichmentWorker:
    """Consumes LeadCreated events and runs the enrichment pipeline.

    Bounded concurrency via asyncio.Semaphore.  Includes an LLM
    circuit breaker: 10 consecutive failures pauses for 5 minutes.
    """

    def __init__(
        self,
        pipeline: "EnrichmentPipeline",
        bus: EventBus,
        max_concurrent: int = 5,
    ) -> None:
        self._pipeline = pipeline
        self._bus = bus
        self._semaphore = asyncio.Semaphore(max_concurrent)
        self._consecutive_failures = 0
        self._circuit_open_until: float = 0
        self._events_processed = 0
        self._last_error: str | None = None

    async def start(self) -> None:
        """Long-running coroutine that consumes LeadCreated events."""
        await self._bus.consume(
            LeadCreated,
            self._handle_event,
            worker_name="enrichment_worker",
        )

    async def _handle_event(self, event: LeadCreated) -> None:
        """Process one LeadCreated event with bounded concurrency."""
        # LLM circuit breaker check
        loop = asyncio.get_event_loop()
        if loop.time() < self._circuit_open_until:
            logger.warning(
                "enrichment_circuit_open",
                lead_id=str(event.lead_id),
                resumes_in_seconds=int(self._circuit_open_until - loop.time()),
            )
            return

        async with self._semaphore:
            log = logger.bind(lead_id=str(event.lead_id), module="enrichment")
            try:
                await self._pipeline.execute(event.lead_id)
                self._consecutive_failures = 0
                self._events_processed += 1
            except AlreadyProcessed:
                log.debug("already_processed")
            except BudgetExceeded:
                log.warning("budget_paused")
            except Exception as exc:
                self._consecutive_failures += 1
                self._last_error = f"{type(exc).__name__}: {exc}"
                log.error("enrichment_failed", error=self._last_error, exc_info=True)

                if self._consecutive_failures >= 10:
                    self._circuit_open_until = loop.time() + 300  # 5 minutes
                    logger.error(
                        "enrichment_circuit_breaker_tripped",
                        consecutive_failures=self._consecutive_failures,
                        pause_seconds=300,
                    )
                    self._consecutive_failures = 0

    @property
    def stats(self) -> dict:
        """Worker stats for the health endpoint."""
        loop = asyncio.get_event_loop()
        return {
            "events_processed": self._events_processed,
            "in_flight": self._semaphore._value,
            "last_error": self._last_error,
            "circuit_open": loop.time() < self._circuit_open_until,
        }


class PendingLeadsResweeper:
    """Periodic task that recovers leads stuck in intermediate statuses.

    This is the safety net that makes the Postgres-as-durable-queue
    pattern work.  If the in-process bus drops events (crash, overflow),
    the resweeper finds them and re-publishes.
    """

    def __init__(
        self,
        repository: object,  # EnrichmentRepository
        bus: EventBus,
        interval_seconds: int = 300,
        older_than_minutes: int = 10,
        batch_size: int = 50,
    ) -> None:
        self._repo = repository
        self._bus = bus
        self._interval = interval_seconds
        self._older_than = older_than_minutes
        self._batch_size = batch_size

    async def start(self) -> None:
        """Run the resweep loop indefinitely."""
        logger.info("resweeper_started", interval=self._interval)
        while True:
            try:
                await self._sweep()
            except asyncio.CancelledError:
                raise
            except Exception:
                logger.error("resweeper_error", exc_info=True)
            await asyncio.sleep(self._interval)

    async def _sweep(self) -> None:
        """One resweep iteration: find stuck leads and republish events."""
        leads = await self._repo.get_pending_leads(  # type: ignore[attr-defined]
            statuses=list(_RESWEEP_STATUSES),
            older_than_minutes=self._older_than,
            limit=self._batch_size,
        )
        if not leads:
            return

        logger.info("resweeper_found", count=len(leads))
        for lead in leads:
            event = LeadCreated(
                lead_id=lead["id"],
                source=lead.get("source", "unknown"),
                signal_type=lead.get("signal_type"),
            )
            await self._bus.publish(event)


async def start_background_workers(
    app: "FastAPI",
    pipeline: "EnrichmentPipeline",
    bus: EventBus,
    repository: object,
    settings: object,
) -> list[asyncio.Task]:
    """Launch background workers and return their tasks for shutdown management."""
    from config import Settings

    s = settings if isinstance(settings, Settings) else Settings()

    worker = EnrichmentWorker(
        pipeline=pipeline,
        bus=bus,
        max_concurrent=s.max_concurrent_enrichments,
    )
    app.state.enrichment_worker = worker

    resweeper = PendingLeadsResweeper(
        repository=repository,
        bus=bus,
        interval_seconds=s.resweeper_interval_seconds,
    )

    tasks = [
        asyncio.create_task(worker.start(), name="enrichment_worker"),
        asyncio.create_task(resweeper.start(), name="resweeper"),
    ]

    # Run initial resweep on startup to recover any leads from previous crash
    asyncio.create_task(resweeper._sweep(), name="startup_resweep")

    return tasks
