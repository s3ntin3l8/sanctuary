"""action items sole owner: enricher; dedup + unique index

Revision ID: a3f1e9b2d4c7
Revises: ed6760d9c7ab
Create Date: 2026-05-14 00:00:00.000000

"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

# revision identifiers, used by Alembic.
revision: str = "a3f1e9b2d4c7"
down_revision: str | Sequence[str] | None = "ed6760d9c7ab"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    # a) Store batch-level detected actions on the batch (enricher reads them as hints)
    op.add_column(
        "ingest_batches", sa.Column("detected_actions", sa.JSON(), nullable=True)
    )

    # b) Remove existing duplicate action_items — keep the highest id per group
    #    (the most recently created row, typically from the most recent analysis pass)
    op.execute(
        """
        DELETE FROM action_items
        WHERE id NOT IN (
            SELECT MAX(id)
            FROM action_items
            WHERE due_date IS NOT NULL
            GROUP BY case_id, due_date, action_type
        )
        AND due_date IS NOT NULL
        """
    )

    # c) Unique index — prevents future duplicates regardless of the creation path
    op.create_index(
        "uq_action_items_case_due_type",
        "action_items",
        ["case_id", "due_date", "action_type"],
        unique=True,
    )


def downgrade() -> None:
    op.drop_index("uq_action_items_case_due_type", table_name="action_items")
    op.drop_column("ingest_batches", "detected_actions")
