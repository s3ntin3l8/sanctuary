"""action_item_superseded_tombstone

Revision ID: 99e487374084
Revises: 1709cde45dde
Create Date: 2026-05-23 00:00:00.000000

Add `superseded` boolean column to action_items.  When a later document
supersedes an action item date (Terminsverlegung / Umladung pattern), the
old item is now marked DISMISSED + superseded=True instead of being deleted.
This tombstone persists across arbitrary doc-processing order, preventing
older documents that enrich after the rescheduling notice from re-inserting
a stale date.
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "99e487374084"
down_revision: str | Sequence[str] | None = "1709cde45dde"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.add_column(
        "action_items",
        sa.Column("superseded", sa.Boolean(), nullable=False, server_default="0"),
    )


def downgrade() -> None:
    op.drop_column("action_items", "superseded")
