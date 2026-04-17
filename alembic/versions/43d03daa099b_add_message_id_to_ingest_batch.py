"""add_message_id_to_ingest_batch

Revision ID: 43d03daa099b
Revises: cc7bed04fc19
Create Date: 2026-04-17 19:56:46.566902

"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

# revision identifiers, used by Alembic.
revision: str = "43d03daa099b"
down_revision: str | Sequence[str] | None = "cc7bed04fc19"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.add_column("ingest_batches", sa.Column("message_id", sa.String(), nullable=True))
    op.create_index(
        op.f("ix_ingest_batches_message_id"),
        "ingest_batches",
        ["message_id"],
        unique=False,
    )


def downgrade() -> None:
    op.drop_index(op.f("ix_ingest_batches_message_id"), table_name="ingest_batches")
    op.drop_column("ingest_batches", "message_id")
