"""add_audit_logs

Revision ID: 751c06b1f6db
Revises: bb4cbacfabda
Create Date: 2026-05-17 18:00:00.000000

"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

# revision identifiers, used by Alembic.
revision: str = "751c06b1f6db"
down_revision: str | Sequence[str] | None = "bb4cbacfabda"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    """Upgrade schema."""
    op.create_table(
        "audit_logs",
        sa.Column("id", sa.Integer(), nullable=False),
        sa.Column(
            "created_at",
            sa.DateTime(),
            nullable=False,
            server_default=sa.text("(strftime('%Y-%m-%dT%H:%M:%S', 'now'))"),
        ),
        sa.Column(
            "actor",
            sa.String(),
            nullable=False,
            server_default="single_user",
        ),
        sa.Column("event_type", sa.String(), nullable=False),
        sa.Column("target_type", sa.String(), nullable=True),
        sa.Column("target_id", sa.String(), nullable=True),
        sa.Column("payload", sa.JSON(), nullable=True),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index(
        "ix_audit_logs_created_at", "audit_logs", ["created_at"], unique=False
    )
    op.create_index(
        "ix_audit_logs_event_type", "audit_logs", ["event_type"], unique=False
    )


def downgrade() -> None:
    """Downgrade schema."""
    op.drop_index("ix_audit_logs_event_type", table_name="audit_logs")
    op.drop_index("ix_audit_logs_created_at", table_name="audit_logs")
    op.drop_table("audit_logs")
