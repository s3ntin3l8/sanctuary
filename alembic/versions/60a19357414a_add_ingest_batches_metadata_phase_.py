"""add ingest_batches metadata_phase_queued_at

Revision ID: 60a19357414a
Revises: f62172cfb232
Create Date: 2026-07-10 23:21:59.920678

"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

# revision identifiers, used by Alembic.
revision: str = "60a19357414a"
down_revision: str | Sequence[str] | None = "f62172cfb232"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    """Upgrade schema."""
    with op.batch_alter_table("ingest_batches", schema=None) as batch_op:
        batch_op.add_column(
            sa.Column("metadata_phase_queued_at", sa.DateTime(), nullable=True)
        )


def downgrade() -> None:
    """Downgrade schema."""
    with op.batch_alter_table("ingest_batches", schema=None) as batch_op:
        batch_op.drop_column("metadata_phase_queued_at")
