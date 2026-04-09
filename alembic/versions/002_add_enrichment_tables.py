"""Add enrichment tables and raw_leads status constraint.

Revision ID: 002
Revises: 001
Create Date: 2026-04-07
"""

from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects.postgresql import ARRAY, UUID

revision: str = "002"
down_revision: Union[str, None] = "001"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.create_table(
        "lead_enrichments",
        sa.Column("lead_id", UUID(as_uuid=True), sa.ForeignKey("raw_leads.id", ondelete="CASCADE"), primary_key=True),
        sa.Column("refined_signal_type", sa.Text()),
        sa.Column("refined_signal_strength", sa.SmallInteger()),
        sa.Column("company_stage", sa.Text()),
        sa.Column("decision_maker_likelihood", sa.SmallInteger()),
        sa.Column("urgency_score", sa.SmallInteger()),
        sa.Column("icp_fit_score", sa.SmallInteger()),
        sa.Column("extracted_stack", ARRAY(sa.Text())),
        sa.Column("pain_summary", sa.Text()),
        sa.Column("recommended_approach", sa.Text(), nullable=False),
        sa.Column("skip_reason", sa.Text()),
        sa.Column("enriched_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.text("NOW()")),
    )

    op.create_table(
        "company_enrichments",
        sa.Column("domain", sa.Text(), primary_key=True),
        sa.Column("homepage_title", sa.Text()),
        sa.Column("is_reachable", sa.Boolean()),
        sa.Column("funding_stage", sa.Text()),
        sa.Column("last_funded_at", sa.Date()),
        sa.Column("employee_count", sa.Integer()),
        sa.Column("enriched_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.text("NOW()")),
        sa.Column("expires_at", sa.DateTime(timezone=True), nullable=False),
    )

    op.create_table(
        "company_resolutions",
        sa.Column("cache_key", sa.Text(), primary_key=True),
        sa.Column("company_name", sa.Text()),
        sa.Column("company_domain", sa.Text()),
        sa.Column("resolved_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.text("NOW()")),
    )

    op.create_table(
        "llm_call_log",
        sa.Column("id", sa.BigInteger(), primary_key=True, autoincrement=True),
        sa.Column("lead_id", UUID(as_uuid=True)),
        sa.Column("stage", sa.Text(), nullable=False),
        sa.Column("model", sa.Text(), nullable=False),
        sa.Column("input_tokens", sa.Integer(), nullable=False),
        sa.Column("output_tokens", sa.Integer(), nullable=False),
        sa.Column("cost_usd", sa.Numeric(10, 6), nullable=False),
        sa.Column("called_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.text("NOW()")),
    )

    op.create_index("idx_llm_log_called", "llm_call_log", [sa.text("called_at DESC")])

    # Status constraint on raw_leads
    op.create_check_constraint(
        "raw_leads_status_check",
        "raw_leads",
        "status IN ('new', 'pending_enrichment', 'enriching', 'scored', "
        "'enrichment_failed', 'budget_paused', 'queued', 'sent', 'closed', 'dead')",
    )


def downgrade() -> None:
    op.drop_constraint("raw_leads_status_check", "raw_leads", type_="check")
    op.drop_index("idx_llm_log_called", table_name="llm_call_log")
    op.drop_table("llm_call_log")
    op.drop_table("company_resolutions")
    op.drop_table("company_enrichments")
    op.drop_table("lead_enrichments")
