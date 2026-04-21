"""Add target_companies and target_people tables for prospect discovery.

These tables hold results from LinkedIn /search-companies and /search-employees —
prospect data, not buying-signal leads. Kept separate from raw_leads so scoring
and enrichment semantics stay clean.

Revision ID: 004
Revises: 003
Create Date: 2026-04-21
"""

from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects.postgresql import ARRAY, JSONB, UUID

revision: str = "004"
down_revision: Union[str, None] = "003"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.create_table(
        "target_companies",
        sa.Column(
            "id",
            UUID(as_uuid=True),
            primary_key=True,
            server_default=sa.text("gen_random_uuid()"),
        ),
        sa.Column("source", sa.Text(), nullable=False),
        sa.Column("source_id", sa.Text(), nullable=False),
        sa.Column("linkedin_url", sa.Text()),
        sa.Column("name", sa.Text(), nullable=False),
        sa.Column("domain", sa.Text()),
        sa.Column("industry", sa.Text()),
        sa.Column("headcount_band", sa.Text()),
        sa.Column("headcount_growth", sa.Integer()),
        sa.Column("annual_revenue_min", sa.Integer()),
        sa.Column("annual_revenue_max", sa.Integer()),
        sa.Column("annual_revenue_currency", sa.Text()),
        sa.Column("hq_location", sa.Text()),
        sa.Column("technologies", ARRAY(sa.Text())),
        sa.Column("hiring_on_linkedin", sa.Boolean()),
        sa.Column("raw_payload", JSONB(), nullable=False),
        sa.Column(
            "discovered_at",
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.text("NOW()"),
        ),
        sa.UniqueConstraint("source", "source_id"),
    )

    op.create_table(
        "target_people",
        sa.Column(
            "id",
            UUID(as_uuid=True),
            primary_key=True,
            server_default=sa.text("gen_random_uuid()"),
        ),
        sa.Column("source", sa.Text(), nullable=False),
        sa.Column("source_id", sa.Text(), nullable=False),
        sa.Column("linkedin_url", sa.Text()),
        sa.Column("full_name", sa.Text(), nullable=False),
        sa.Column("headline", sa.Text()),
        sa.Column("current_title", sa.Text()),
        sa.Column("current_company", sa.Text()),
        sa.Column("current_company_domain", sa.Text()),
        sa.Column("location", sa.Text()),
        sa.Column("seed_url", sa.Text()),
        sa.Column("raw_payload", JSONB(), nullable=False),
        sa.Column(
            "discovered_at",
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.text("NOW()"),
        ),
        sa.UniqueConstraint("source", "source_id"),
    )

    op.create_index("idx_target_companies_domain", "target_companies", ["domain"])
    op.create_index(
        "idx_target_people_company",
        "target_people",
        ["current_company_domain"],
    )


def downgrade() -> None:
    op.drop_index("idx_target_people_company", table_name="target_people")
    op.drop_index("idx_target_companies_domain", table_name="target_companies")
    op.drop_table("target_people")
    op.drop_table("target_companies")
