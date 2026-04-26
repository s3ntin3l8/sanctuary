"""drop saved_searches table

Revision ID: a1b2c3d4e5f6
Revises: 5440cdef505b
Create Date: 2026-04-26 00:00:00.000000

"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "f9e8d7c6b5a4"
down_revision: str | Sequence[str] | None = "5440cdef505b"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.drop_table("saved_searches")


def downgrade() -> None:
    op.create_table(
        "saved_searches",
        sa.Column("id", sa.Integer(), nullable=False),
        sa.Column("user_id", sa.String(), nullable=False),
        sa.Column("name", sa.String(), nullable=False),
        sa.Column("filter_json", sa.JSON(), nullable=False),
        sa.Column("ingest_date", sa.DateTime(), nullable=True),
        sa.PrimaryKeyConstraint("id"),
    )
