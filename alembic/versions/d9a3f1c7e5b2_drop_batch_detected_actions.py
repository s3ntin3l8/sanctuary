"""drop ingest_batches.detected_actions

Batch analysis no longer extracts deadlines; the per-document enricher is the
only source of ActionItem rows.

Revision ID: d9a3f1c7e5b2
Revises: c2d7a9e4b1f6
Create Date: 2026-10-08 15:30:00.000000

"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

# revision identifiers, used by Alembic.
revision: str = "d9a3f1c7e5b2"  # pragma: allowlist secret
down_revision: str | Sequence[str] | None = "c2d7a9e4b1f6"  # pragma: allowlist secret
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.drop_column("ingest_batches", "detected_actions")


def downgrade() -> None:
    op.add_column(
        "ingest_batches",
        sa.Column("detected_actions", postgresql.JSONB(), nullable=True),
    )
