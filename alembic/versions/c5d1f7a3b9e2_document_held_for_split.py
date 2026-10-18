"""documents.held_for_split_at: a packet-like email attachment waits for a split decision

Revision ID: c5d1f7a3b9e2
Revises: a7c3e9f1b5d2
Create Date: 2026-10-18 12:00:00.000000

"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

# revision identifiers, used by Alembic.
revision: str = "c5d1f7a3b9e2"  # pragma: allowlist secret
down_revision: str | Sequence[str] | None = "a7c3e9f1b5d2"  # pragma: allowlist secret
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.add_column(
        "documents", sa.Column("held_for_split_at", sa.DateTime(), nullable=True)
    )
    op.create_index(
        "ix_documents_held_for_split_at",
        "documents",
        ["held_for_split_at"],
        postgresql_where=sa.text("held_for_split_at IS NOT NULL"),
    )


def downgrade() -> None:
    op.drop_index("ix_documents_held_for_split_at", table_name="documents")
    op.drop_column("documents", "held_for_split_at")
