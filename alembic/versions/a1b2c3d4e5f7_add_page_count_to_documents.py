"""add_page_count_to_documents

Revision ID: a1b2c3d4e5f7
Revises: f9e8d7c6b5a4
Create Date: 2026-04-28 00:00:00.000000

"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "a1b2c3d4e5f7"
down_revision: str | Sequence[str] | None = "f9e8d7c6b5a4"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.add_column(
        "documents",
        sa.Column("page_count", sa.Integer(), nullable=False, server_default="0"),
    )


def downgrade() -> None:
    op.drop_column("documents", "page_count")
