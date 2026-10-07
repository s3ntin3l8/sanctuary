"""home_briefings: one generated-locally morning briefing per user and day

Revision ID: d4e1f2a3b5c6
Revises: c7c7bfc79adf
Create Date: 2026-10-07 17:40:00.000000

"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

# revision identifiers, used by Alembic.
revision: str = "d4e1f2a3b5c6"  # pragma: allowlist secret
down_revision: str | Sequence[str] | None = "c7c7bfc79adf"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "home_briefings",
        sa.Column("id", sa.Integer(), nullable=False),
        sa.Column("user_id", sa.Integer(), nullable=False),
        sa.Column("day", sa.Date(), nullable=False),
        sa.Column("status", sa.String(), nullable=False),
        sa.Column("summary", sa.Text(), nullable=True),
        sa.Column("priorities", postgresql.JSONB(astext_type=sa.Text()), nullable=True),
        sa.Column("model_label", sa.String(), nullable=True),
        sa.Column("external", sa.Boolean(), nullable=False, server_default=sa.false()),
        sa.Column("error", sa.Text(), nullable=True),
        sa.Column("generated_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("queued_at", sa.DateTime(timezone=True), nullable=True),
        sa.ForeignKeyConstraint(["user_id"], ["users.id"], ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("user_id", "day", name="uq_home_briefings_user_day"),
    )
    op.create_index(
        op.f("ix_home_briefings_user_id"), "home_briefings", ["user_id"], unique=False
    )


def downgrade() -> None:
    op.drop_index(op.f("ix_home_briefings_user_id"), table_name="home_briefings")
    op.drop_table("home_briefings")
