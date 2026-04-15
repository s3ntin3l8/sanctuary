"""Add ingest status fields to Document

Revision ID: afa8c6ca0cf2
Revises: 9e4cfaaa0c42
Create Date: 2026-04-15 18:16:32.597264

"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

# revision identifiers, used by Alembic.
revision: str = "afa8c6ca0cf2"
down_revision: str | Sequence[str] | None = "9e4cfaaa0c42"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    """Upgrade schema."""
    op.add_column("documents", sa.Column("ingest_status", sa.String(10), nullable=True))
    op.add_column("documents", sa.Column("ingest_error", sa.Text(), nullable=True))
    op.add_column(
        "documents", sa.Column("ingest_started_at", sa.DateTime(), nullable=True)
    )
    op.add_column(
        "documents", sa.Column("ingest_completed_at", sa.DateTime(), nullable=True)
    )

    # Set default values for existing records
    op.execute(
        "UPDATE documents SET ingest_status = 'completed' WHERE content IS NOT NULL AND ingest_status IS NULL"
    )
    op.execute(
        "UPDATE documents SET ingest_status = 'pending' WHERE content IS NULL AND ingest_status IS NULL"
    )


def downgrade() -> None:
    """Downgrade schema."""
    op.drop_column("documents", "ingest_completed_at")
    op.drop_column("documents", "ingest_started_at")
    op.drop_column("documents", "ingest_error")
    op.drop_column("documents", "ingest_status")
