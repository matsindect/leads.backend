"""ScrapeOrchestrator — runs one adapter end-to-end.

Depends only on domain protocols (SourceAdapter, LeadRepository,
EventPublisher).  Concrete implementations are injected at construction.
"""

from __future__ import annotations

import time
import uuid
from datetime import UTC, datetime

import structlog

from config import Settings
from domain.interfaces import EventPublisher, LeadRepository, SourceAdapter
from domain.models import CanonicalLead, RunReport

logger = structlog.get_logger()


class ScrapeOrchestrator:
    """Coordinates a single scrape pass: fetch → normalize → dedup-insert → emit events."""

    def __init__(
        self,
        repository: LeadRepository,
        publisher: EventPublisher,
        settings: Settings,
    ) -> None:
        self._repository = repository
        self._publisher = publisher
        self._settings = settings

    async def run(self, adapter: SourceAdapter) -> RunReport:
        """Execute one full scrape cycle for *adapter*.

        Returns a RunReport regardless of success or failure.
        """
        run_id = uuid.uuid4()
        log = logger.bind(adapter=adapter.name, run_id=str(run_id))
        started_at = datetime.now(UTC)
        start_ns = time.monotonic_ns()

        # Circuit breaker check
        if await self._is_circuit_open(adapter.name, log):
            return self._error_report(
                adapter.name, run_id, started_at, start_ns,
                error="circuit_breaker_open",
            )

        fetched_count = 0
        normalized: list[CanonicalLead] = []
        error_msg: str | None = None

        try:
            log.info("scrape_started")
            raw_records = await adapter.fetch_raw()
            fetched_count = len(raw_records)
            log.info("fetch_complete", fetched=fetched_count)

            for raw in raw_records:
                try:
                    lead = adapter.normalize(raw)
                    if lead is not None:
                        normalized.append(lead)
                except Exception:
                    log.warning("normalize_error", raw_id=raw.get("id", "unknown"), exc_info=True)

            log.info("normalize_complete", normalized=len(normalized))

            inserted_ids, duplicates = await self._repository.insert_leads(normalized)
            log.info("insert_complete", inserted=len(inserted_ids), duplicates=duplicates)

            # Emit events for newly inserted leads
            if inserted_ids:
                signal = (
                    normalized[0].signal_type.value
                    if normalized and normalized[0].signal_type
                    else None
                )
                await self._publisher.publish_new_leads(
                    inserted_ids, adapter.name, signal
                )
                log.info("events_published", count=len(inserted_ids))

        except Exception as exc:
            error_msg = f"{type(exc).__name__}: {exc}"
            log.error("scrape_failed", error=error_msg, exc_info=True)

        duration_ms = (time.monotonic_ns() - start_ns) // 1_000_000
        report = RunReport(
            adapter_name=adapter.name,
            run_id=run_id,
            fetched=fetched_count,
            normalized=len(normalized),
            inserted=len(inserted_ids) if error_msg is None else 0,
            duplicates=duplicates if error_msg is None else 0,
            errors=1 if error_msg else 0,
            duration_ms=duration_ms,
            started_at=started_at,
            error=error_msg,
        )

        await self._repository.record_run(report)
        log.info("scrape_complete", duration_ms=duration_ms)
        return report

    async def _is_circuit_open(self, adapter_name: str, log: structlog.stdlib.BoundLogger) -> bool:  # type: ignore[type-arg]
        """Check if the adapter's circuit breaker is tripped.

        Failures older than ``circuit_breaker_cooldown_seconds`` are ignored,
        so the breaker auto-resets after the cooldown window expires.
        """
        failures = await self._repository.count_recent_failures(
            adapter_name,
            self._settings.circuit_breaker_threshold,
            within_seconds=self._settings.circuit_breaker_cooldown_seconds,
        )
        if failures >= self._settings.circuit_breaker_threshold:
            log.warning("circuit_breaker_open", consecutive_failures=failures)
            return True
        return False

    @staticmethod
    def _error_report(
        adapter_name: str,
        run_id: uuid.UUID,
        started_at: datetime,
        start_ns: int,
        *,
        error: str,
    ) -> RunReport:
        duration_ms = (time.monotonic_ns() - start_ns) // 1_000_000
        return RunReport(
            adapter_name=adapter_name,
            run_id=run_id,
            fetched=0,
            normalized=0,
            inserted=0,
            duplicates=0,
            errors=1,
            duration_ms=duration_ms,
            started_at=started_at,
            error=error,
        )
