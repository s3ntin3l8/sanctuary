"""drop legacy Case.court_id column

Revision ID: cc7bed04fc19
Revises: 47e3c9c59e84
Create Date: 2026-04-16 00:00:03.000000

Per-court Aktenzeichen now lives on Proceeding.az_court. The Case-level
court_id column is redundant. Pre-release cleanup: drop it rather than
leave deprecated.
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "cc7bed04fc19"
down_revision: str | Sequence[str] | None = "47e3c9c59e84"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    with op.batch_alter_table("cases") as batch_op:
        batch_op.drop_column("court_id")


def downgrade() -> None:
    with op.batch_alter_table("cases") as batch_op:
        batch_op.add_column(sa.Column("court_id", sa.String(), nullable=True))
