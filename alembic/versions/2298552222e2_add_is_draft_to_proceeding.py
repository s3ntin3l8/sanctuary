"""add is_draft to proceeding

Revision ID: 2298552222e2
Revises: a8b9c0d1e2f3
Create Date: 2026-05-06 20:37:58.594033

"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

# revision identifiers, used by Alembic.
revision: str = "2298552222e2"
down_revision: str | Sequence[str] | None = "a8b9c0d1e2f3"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    """Add is_draft flag to proceedings (mirrors cases.is_draft)."""
    with op.batch_alter_table("proceedings", schema=None) as batch_op:
        batch_op.add_column(
            sa.Column(
                "is_draft",
                sa.Boolean(),
                nullable=False,
                server_default=sa.false(),
            )
        )


def downgrade() -> None:
    with op.batch_alter_table("proceedings", schema=None) as batch_op:
        batch_op.drop_column("is_draft")
