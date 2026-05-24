"""add addressee to action_items

Revision ID: 460a2a27f371
Revises: 99e487374084
Create Date: 2026-05-24 22:24:31.386576

"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

# revision identifiers, used by Alembic.
revision: str = "460a2a27f371"
down_revision: str | Sequence[str] | None = "99e487374084"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    """Add addressee column so action-item filtering can distinguish user-directed
    items from those aimed at opposing/third-party/court."""
    op.add_column(
        "action_items",
        sa.Column("addressee", sa.String(), nullable=True),
    )


def downgrade() -> None:
    op.drop_column("action_items", "addressee")
