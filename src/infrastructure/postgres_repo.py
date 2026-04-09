"""PostgreSQL implementation of LeadRepository and EnrichmentRepository.

Uses SQLAlchemy Core for insert performance and explicit SQL control.
Satisfies ``domain.interfaces.LeadRepository`` and ``domain.interfaces.EnrichmentRepository``.
"""

from __future__ import annotations

import uuid
from collections.abc import Sequence
from datetime import UTC, date, datetime, timedelta
from typing import Any

import sqlalchemy as sa
from sqlalchemy import (
    BigInteger,
    Boolean,
    Column,
    Date,
    DateTime,
    ForeignKey,
    Integer,
    MetaData,
    Numeric,
    SmallInteger,
    Table,
    Text,
    text,
)
from sqlalchemy.dialects.postgresql import ARRAY, JSONB, UUID
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from domain.models import (
    AdapterHealth,
    AdapterInfo,
    CanonicalLead,
    RunReport,
)
from modules.scraping.dedup import compute_dedup_hash

metadata = MetaData()

raw_leads_table = Table(
    "raw_leads",
    metadata,
    Column("id", UUID(as_uuid=True), primary_key=True, server_default=text("gen_random_uuid()")),
    Column("source", Text, nullable=False),
    Column("source_id", Text, nullable=False),
    Column("dedup_hash", Text, nullable=False, unique=True),
    Column("url", Text, nullable=False),
    Column("signal_type", Text),
    Column("signal_strength", SmallInteger),
    Column("title", Text),
    Column("body", Text),
    Column("raw_payload", JSONB, nullable=False),
    Column("company_name", Text),
    Column("company_domain", Text),
    Column("person_name", Text),
    Column("person_role", Text),
    Column("location", Text),
    Column("stack_mentions", ARRAY(Text)),
    Column("posted_at", DateTime(timezone=True)),
    Column("fetched_at", DateTime(timezone=True), nullable=False, server_default=text("NOW()")),
    Column("enriched_at", DateTime(timezone=True)),
    Column("scored_at", DateTime(timezone=True)),
    Column("score", Numeric(5, 2)),
    Column("status", Text, nullable=False, server_default=text("'new'")),
    sa.UniqueConstraint("source", "source_id"),
)

scrape_runs_table = Table(
    "scrape_runs",
    metadata,
    Column("id", UUID(as_uuid=True), primary_key=True, server_default=text("gen_random_uuid()")),
    Column("adapter", Text, nullable=False),
    Column("run_id", UUID(as_uuid=True), nullable=False),
    Column("started_at", DateTime(timezone=True), nullable=False),
    Column("duration_ms", Integer, nullable=False),
    Column("fetched", Integer, nullable=False, server_default=text("0")),
    Column("inserted", Integer, nullable=False, server_default=text("0")),
    Column("duplicates", Integer, nullable=False, server_default=text("0")),
    Column("errors", Integer, nullable=False, server_default=text("0")),
    Column("error", Text),
    Column("status", Text, nullable=False, server_default=text("'success'")),
)


lead_enrichments_table = Table(
    "lead_enrichments",
    metadata,
    Column(
        "lead_id", UUID(as_uuid=True),
        ForeignKey("raw_leads.id", ondelete="CASCADE"),
        primary_key=True,
    ),
    Column("refined_signal_type", Text),
    Column("refined_signal_strength", SmallInteger),
    Column("company_stage", Text),
    Column("decision_maker_likelihood", SmallInteger),
    Column("urgency_score", SmallInteger),
    Column("icp_fit_score", SmallInteger),
    Column("extracted_stack", ARRAY(Text)),
    Column("pain_summary", Text),
    Column("recommended_approach", Text, nullable=False),
    Column("skip_reason", Text),
    Column("enriched_at", DateTime(timezone=True), nullable=False, server_default=text("NOW()")),
)

company_enrichments_table = Table(
    "company_enrichments",
    metadata,
    Column("domain", Text, primary_key=True),
    Column("homepage_title", Text),
    Column("is_reachable", Boolean),
    Column("funding_stage", Text),
    Column("last_funded_at", Date),
    Column("employee_count", Integer),
    Column("enriched_at", DateTime(timezone=True), nullable=False, server_default=text("NOW()")),
    Column("expires_at", DateTime(timezone=True), nullable=False),
)

company_resolutions_table = Table(
    "company_resolutions",
    metadata,
    Column("cache_key", Text, primary_key=True),
    Column("company_name", Text),
    Column("company_domain", Text),
    Column("resolved_at", DateTime(timezone=True), nullable=False, server_default=text("NOW()")),
)

llm_call_log_table = Table(
    "llm_call_log",
    metadata,
    Column("id", BigInteger, primary_key=True, autoincrement=True),
    Column("lead_id", UUID(as_uuid=True)),
    Column("stage", Text, nullable=False),
    Column("model", Text, nullable=False),
    Column("input_tokens", Integer, nullable=False),
    Column("output_tokens", Integer, nullable=False),
    Column("cost_usd", Numeric(10, 6), nullable=False),
    Column("called_at", DateTime(timezone=True), nullable=False, server_default=text("NOW()")),
)


class PostgresLeadRepository:
    """Concrete LeadRepository backed by PostgreSQL.

    Injected with a session factory — never creates its own connections.
    """

    def __init__(self, session_factory: async_sessionmaker[AsyncSession]) -> None:
        self._session_factory = session_factory

    async def insert_leads(
        self, leads: Sequence[CanonicalLead]
    ) -> tuple[list[uuid.UUID], int]:
        """Insert leads using ON CONFLICT DO NOTHING for dedup safety."""
        if not leads:
            return [], 0

        inserted_ids: list[uuid.UUID] = []
        duplicates = 0

        async with self._session_factory() as session, session.begin():
            for lead in leads:
                lead_id = uuid.uuid4()
                dedup_hash = compute_dedup_hash(lead)
                stmt = (
                    sa.dialects.postgresql.insert(raw_leads_table)
                    .values(
                        id=lead_id,
                        source=lead.source,
                        source_id=lead.source_id,
                        dedup_hash=dedup_hash,
                        url=lead.url,
                        signal_type=lead.signal_type.value if lead.signal_type else None,
                        signal_strength=lead.signal_strength,
                        title=lead.title,
                        body=lead.body,
                        raw_payload=_json_safe(lead.raw_payload),
                        company_name=lead.company_name,
                        company_domain=lead.company_domain,
                        person_name=lead.person_name,
                        person_role=lead.person_role,
                        location=lead.location,
                        stack_mentions=lead.stack_mentions,
                        posted_at=lead.posted_at,
                        status="new",
                    )
                    .on_conflict_do_nothing(index_elements=["dedup_hash"])
                    .returning(raw_leads_table.c.id)
                )
                result = await session.execute(stmt)
                row = result.fetchone()
                if row is not None:
                    inserted_ids.append(row[0])
                else:
                    duplicates += 1

        return inserted_ids, duplicates

    async def record_run(self, report: RunReport) -> None:
        """Persist a completed scrape run."""
        status = "error" if report.error else "success"
        async with self._session_factory() as session, session.begin():
            await session.execute(
                scrape_runs_table.insert().values(
                    run_id=report.run_id,
                    adapter=report.adapter_name,
                    started_at=report.started_at,
                    duration_ms=report.duration_ms,
                    fetched=report.fetched,
                    inserted=report.inserted,
                    duplicates=report.duplicates,
                    errors=report.errors,
                    error=report.error,
                    status=status,
                )
            )

    async def get_adapter_info(self, adapter_name: str) -> AdapterInfo | None:
        """Fetch the most recent run for one adapter."""
        async with self._session_factory() as session:
            result = await session.execute(
                sa.select(
                    scrape_runs_table.c.started_at,
                    scrape_runs_table.c.status,
                )
                .where(scrape_runs_table.c.adapter == adapter_name)
                .order_by(scrape_runs_table.c.started_at.desc())
                .limit(1)
            )
            row = result.fetchone()
            if row is None:
                return None
            return AdapterInfo(
                name=adapter_name,
                poll_interval_seconds=0,  # filled by caller with adapter config
                last_run_at=row.started_at,
                last_status=row.status,
            )

    async def get_all_adapter_info(
        self, adapter_names: Sequence[str]
    ) -> list[AdapterInfo]:
        """Fetch last-run info for all adapters."""
        results: list[AdapterInfo] = []
        for name in adapter_names:
            info = await self.get_adapter_info(name)
            results.append(
                info
                or AdapterInfo(
                    name=name, poll_interval_seconds=0, last_run_at=None, last_status=None
                )
            )
        return results

    async def get_adapter_health(self, adapter_name: str) -> AdapterHealth:
        """Compute detailed health for one adapter."""
        async with self._session_factory() as session:
            # Last successful run
            success_result = await session.execute(
                sa.select(scrape_runs_table.c.started_at)
                .where(
                    scrape_runs_table.c.adapter == adapter_name,
                    scrape_runs_table.c.status == "success",
                )
                .order_by(scrape_runs_table.c.started_at.desc())
                .limit(1)
            )
            success_row = success_result.fetchone()

            # Last error
            error_result = await session.execute(
                sa.select(scrape_runs_table.c.error)
                .where(
                    scrape_runs_table.c.adapter == adapter_name,
                    scrape_runs_table.c.status == "error",
                )
                .order_by(scrape_runs_table.c.started_at.desc())
                .limit(1)
            )
            error_row = error_result.fetchone()

            # Records in last 24h
            cutoff = datetime.now(UTC) - timedelta(hours=24)
            count_result = await session.execute(
                sa.select(sa.func.coalesce(sa.func.sum(scrape_runs_table.c.inserted), 0))
                .where(
                    scrape_runs_table.c.adapter == adapter_name,
                    scrape_runs_table.c.started_at >= cutoff,
                )
            )
            records_24h = int(count_result.scalar_one())

            # Circuit breaker state
            consecutive_failures = await self._count_recent_failures_inner(
                session, adapter_name, 3
            )

        return AdapterHealth(
            name=adapter_name,
            last_success_at=success_row.started_at if success_row else None,
            last_error=error_row.error if error_row else None,
            records_last_24h=records_24h,
            circuit_open=consecutive_failures >= 3,
        )

    async def get_all_adapter_health(
        self, adapter_names: Sequence[str]
    ) -> list[AdapterHealth]:
        """Detailed health for all adapters."""
        return [await self.get_adapter_health(name) for name in adapter_names]

    async def count_recent_failures(self, adapter_name: str, limit: int) -> int:
        """Count consecutive recent failures for circuit-breaker logic."""
        async with self._session_factory() as session:
            return await self._count_recent_failures_inner(session, adapter_name, limit)

    @staticmethod
    async def _count_recent_failures_inner(
        session: AsyncSession, adapter_name: str, limit: int
    ) -> int:
        """Shared implementation — counts consecutive failures from most recent run."""
        result = await session.execute(
            sa.select(scrape_runs_table.c.status)
            .where(scrape_runs_table.c.adapter == adapter_name)
            .order_by(scrape_runs_table.c.started_at.desc())
            .limit(limit)
        )
        rows = result.fetchall()
        count = 0
        for row in rows:
            if row.status == "error":
                count += 1
            else:
                break
        return count

    async def check_connectivity(self) -> bool:
        """Verify database is reachable."""
        try:
            async with self._session_factory() as session:
                await session.execute(text("SELECT 1"))
            return True
        except Exception:
            return False

    # ------------------------------------------------------------------
    # EnrichmentRepository methods
    # ------------------------------------------------------------------

    async def get_lead_by_id(self, lead_id: uuid.UUID) -> dict[str, Any] | None:
        """Fetch a raw_leads row by primary key."""
        async with self._session_factory() as session:
            result = await session.execute(
                sa.select(raw_leads_table).where(raw_leads_table.c.id == lead_id)
            )
            row = result.fetchone()
            return dict(row._mapping) if row else None

    async def get_lead_status(self, lead_id: uuid.UUID) -> str | None:
        """Return the current status of a lead, or None if not found."""
        async with self._session_factory() as session:
            result = await session.execute(
                sa.select(raw_leads_table.c.status).where(raw_leads_table.c.id == lead_id)
            )
            row = result.fetchone()
            return row.status if row else None

    async def update_lead_status(self, lead_id: uuid.UUID, status: str) -> None:
        """Update the status column on raw_leads."""
        async with self._session_factory() as session, session.begin():
            await session.execute(
                raw_leads_table.update()
                .where(raw_leads_table.c.id == lead_id)
                .values(status=status)
            )

    async def upsert_enrichment(self, lead_id: uuid.UUID, data: dict[str, Any]) -> None:
        """Insert or update a row in lead_enrichments."""
        async with self._session_factory() as session, session.begin():
            stmt = (
                sa.dialects.postgresql.insert(lead_enrichments_table)
                .values(lead_id=lead_id, **data)
                .on_conflict_do_update(
                    index_elements=["lead_id"],
                    set_=data,
                )
            )
            await session.execute(stmt)

    async def update_lead_scores(
        self,
        lead_id: uuid.UUID,
        *,
        score: float,
        enriched_at: datetime,
        scored_at: datetime,
    ) -> None:
        """Update raw_leads with enrichment timestamps and final score."""
        async with self._session_factory() as session, session.begin():
            await session.execute(
                raw_leads_table.update()
                .where(raw_leads_table.c.id == lead_id)
                .values(
                    score=score,
                    enriched_at=enriched_at,
                    scored_at=scored_at,
                    status="scored",
                )
            )

    async def get_cached_company(self, domain: str) -> dict[str, Any] | None:
        """Fetch a company_enrichments row if not expired."""
        async with self._session_factory() as session:
            result = await session.execute(
                sa.select(company_enrichments_table).where(
                    company_enrichments_table.c.domain == domain,
                    company_enrichments_table.c.expires_at > datetime.now(UTC),
                )
            )
            row = result.fetchone()
            return dict(row._mapping) if row else None

    async def cache_company(self, domain: str, data: dict[str, Any]) -> None:
        """Upsert a company_enrichments row with expiry."""
        async with self._session_factory() as session, session.begin():
            stmt = (
                sa.dialects.postgresql.insert(company_enrichments_table)
                .values(domain=domain, **data)
                .on_conflict_do_update(
                    index_elements=["domain"],
                    set_=data,
                )
            )
            await session.execute(stmt)

    async def get_cached_resolution(self, cache_key: str) -> dict[str, Any] | None:
        """Fetch a company_resolutions row."""
        async with self._session_factory() as session:
            result = await session.execute(
                sa.select(company_resolutions_table).where(
                    company_resolutions_table.c.cache_key == cache_key
                )
            )
            row = result.fetchone()
            return dict(row._mapping) if row else None

    async def cache_resolution(
        self, cache_key: str, company_name: str | None, company_domain: str | None
    ) -> None:
        """Insert a company_resolutions cache entry."""
        async with self._session_factory() as session, session.begin():
            stmt = (
                sa.dialects.postgresql.insert(company_resolutions_table)
                .values(
                    cache_key=cache_key,
                    company_name=company_name,
                    company_domain=company_domain,
                )
                .on_conflict_do_nothing(index_elements=["cache_key"])
            )
            await session.execute(stmt)

    async def log_llm_call(
        self,
        *,
        lead_id: uuid.UUID | None,
        stage: str,
        model: str,
        input_tokens: int,
        output_tokens: int,
        cost_usd: float,
    ) -> None:
        """Append a row to llm_call_log for cost tracking."""
        async with self._session_factory() as session, session.begin():
            await session.execute(
                llm_call_log_table.insert().values(
                    lead_id=lead_id,
                    stage=stage,
                    model=model,
                    input_tokens=input_tokens,
                    output_tokens=output_tokens,
                    cost_usd=cost_usd,
                )
            )

    async def get_daily_llm_cost(self, day: date | None = None) -> float:
        """Sum cost_usd for a given day (default: today)."""
        target = day or date.today()
        day_start = datetime.combine(target, datetime.min.time(), tzinfo=UTC)
        day_end = day_start + timedelta(days=1)
        async with self._session_factory() as session:
            result = await session.execute(
                sa.select(
                    sa.func.coalesce(sa.func.sum(llm_call_log_table.c.cost_usd), 0)
                ).where(
                    llm_call_log_table.c.called_at >= day_start,
                    llm_call_log_table.c.called_at < day_end,
                )
            )
            return float(result.scalar_one())

    async def get_cost_aggregation(self) -> list[dict[str, Any]]:
        """Return cost aggregated by day, stage, model."""
        async with self._session_factory() as session:
            result = await session.execute(
                sa.select(
                    sa.func.date_trunc("day", llm_call_log_table.c.called_at).label("day"),
                    llm_call_log_table.c.stage,
                    llm_call_log_table.c.model,
                    sa.func.sum(llm_call_log_table.c.cost_usd).label("total_cost"),
                    sa.func.sum(llm_call_log_table.c.input_tokens).label("total_input_tokens"),
                    sa.func.sum(llm_call_log_table.c.output_tokens).label("total_output_tokens"),
                    sa.func.count().label("call_count"),
                )
                .group_by("day", llm_call_log_table.c.stage, llm_call_log_table.c.model)
                .order_by(sa.text("day DESC"))
            )
            return [dict(row._mapping) for row in result.fetchall()]

    async def get_pending_leads(
        self, statuses: Sequence[str], older_than_minutes: int, limit: int
    ) -> list[dict[str, Any]]:
        """Find leads stuck in intermediate statuses for resweep."""
        cutoff = datetime.now(UTC) - timedelta(minutes=older_than_minutes)
        async with self._session_factory() as session:
            result = await session.execute(
                sa.select(raw_leads_table)
                .where(
                    raw_leads_table.c.status.in_(statuses),
                    raw_leads_table.c.fetched_at < cutoff,
                )
                .limit(limit)
            )
            return [dict(row._mapping) for row in result.fetchall()]


def _json_safe(value: Any) -> Any:
    """Recursively convert datetimes to ISO strings so JSONB serialization works.

    Adapters often pass through raw API responses that contain datetime objects
    (e.g. RSS feed parsed dates).  Postgres' JSONB encoder can't serialize
    those — convert them once here at the persistence boundary.
    """
    if isinstance(value, datetime):
        return value.isoformat()
    if isinstance(value, dict):
        return {k: _json_safe(v) for k, v in value.items()}
    if isinstance(value, (list, tuple)):
        return [_json_safe(v) for v in value]
    return value
