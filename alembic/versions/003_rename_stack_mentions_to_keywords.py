"""Rename raw_leads.stack_mentions to keywords.

The field is now used generically for extracted keywords (any vertical),
not just developer tech stack. The rename preserves data.

Revision ID: 003
Revises: 002
Create Date: 2026-04-21
"""

from typing import Sequence, Union

from alembic import op

revision: str = "003"
down_revision: Union[str, None] = "002"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.alter_column("raw_leads", "stack_mentions", new_column_name="keywords")


def downgrade() -> None:
    op.alter_column("raw_leads", "keywords", new_column_name="stack_mentions")
