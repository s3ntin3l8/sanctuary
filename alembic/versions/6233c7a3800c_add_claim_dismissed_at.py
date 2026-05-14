"""Add Claim.dismissed_at

Revision ID: 6233c7a3800c
Revises: 316dd3b3573b
Create Date: 2026-05-14 19:40:44.280600

"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "6233c7a3800c"
down_revision: str | Sequence[str] | None = "316dd3b3573b"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    with op.batch_alter_table("claims", schema=None) as batch_op:
        batch_op.add_column(sa.Column("dismissed_at", sa.DateTime(), nullable=True))
        batch_op.create_index(
            batch_op.f("ix_claims_dismissed_at"), ["dismissed_at"], unique=False
        )


def downgrade() -> None:
    with op.batch_alter_table("claims", schema=None) as batch_op:
        batch_op.drop_index(batch_op.f("ix_claims_dismissed_at"))
        batch_op.drop_column("dismissed_at")
