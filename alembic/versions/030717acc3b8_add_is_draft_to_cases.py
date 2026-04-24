"""add_is_draft_to_cases

Revision ID: 030717acc3b8
Revises: a1b2c3d4e5f6
Create Date: 2026-04-24 13:00:57.441183

"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "030717acc3b8"
down_revision: str | Sequence[str] | None = "a1b2c3d4e5f6"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    with op.batch_alter_table("cases", schema=None) as batch_op:
        batch_op.add_column(
            sa.Column(
                "is_draft", sa.Boolean(), nullable=False, server_default=sa.false()
            )
        )


def downgrade() -> None:
    with op.batch_alter_table("cases", schema=None) as batch_op:
        batch_op.drop_column("is_draft")
