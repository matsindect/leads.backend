"""Create raw_leads and scrape_runs tables.

Revision ID: 001
Revises: None
Create Date: 2026-04-07
"""

from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects.postgresql import ARRAY, JSONB, UUID

revision: str = "001"
down_revision: Union[str, None] = None
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.create_table(
        "raw_leads",
        sa.Column("id", UUID(as_uuid=True), primary_key=True, server_default=sa.text("gen_random_uuid()")),
        sa.Column("source", sa.Text(), nullable=False),
        sa.Column("source_id", sa.Text(), nullable=False),
        sa.Column("dedup_hash", sa.Text(), nullable=False),
        sa.Column("url", sa.Text(), nullable=False),
        sa.Column("signal_type", sa.Text()),
        sa.Column("signal_strength", sa.SmallInteger()),
        sa.Column("title", sa.Text()),
        sa.Column("body", sa.Text()),
        sa.Column("raw_payload", JSONB(), nullable=False),
        sa.Column("company_name", sa.Text()),
        sa.Column("company_domain", sa.Text()),
        sa.Column("person_name", sa.Text()),
        sa.Column("person_role", sa.Text()),
        sa.Column("location", sa.Text()),
        sa.Column("stack_mentions", ARRAY(sa.Text())),
        sa.Column("posted_at", sa.DateTime(timezone=True)),
        sa.Column("fetched_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.text("NOW()")),
        sa.Column("enriched_at", sa.DateTime(timezone=True)),
        sa.Column("scored_at", sa.DateTime(timezone=True)),
        sa.Column("score", sa.Numeric(5, 2)),
        sa.Column("status", sa.Text(), nullable=False, server_default=sa.text("'new'")),
        sa.UniqueConstraint("source", "source_id"),
        sa.UniqueConstraint("dedup_hash"),
    )

    op.create_table(
        "scrape_runs",
        sa.Column("id", UUID(as_uuid=True), primary_key=True, server_default=sa.text("gen_random_uuid()")),
        sa.Column("adapter", sa.Text(), nullable=False),
        sa.Column("run_id", UUID(as_uuid=True), nullable=False),
        sa.Column("started_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("duration_ms", sa.Integer(), nullable=False),
        sa.Column("fetched", sa.Integer(), nullable=False, server_default=sa.text("0")),
        sa.Column("inserted", sa.Integer(), nullable=False, server_default=sa.text("0")),
        sa.Column("duplicates", sa.Integer(), nullable=False, server_default=sa.text("0")),
        sa.Column("errors", sa.Integer(), nullable=False, server_default=sa.text("0")),
        sa.Column("error", sa.Text()),
        sa.Column("status", sa.Text(), nullable=False, server_default=sa.text("'success'")),
    )

    # Index for circuit-breaker queries
    op.create_index(
        "ix_scrape_runs_adapter_started",
        "scrape_runs",
        ["adapter", sa.text("started_at DESC")],
    )


def downgrade() -> None:
    op.drop_index("ix_scrape_runs_adapter_started", table_name="scrape_runs")
    op.drop_table("scrape_runs")
    op.drop_table("raw_leads")
