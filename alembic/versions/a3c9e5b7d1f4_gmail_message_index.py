"""gmail_message_index: metadata mirror of the allowlisted mailbox for the import page

Revision ID: a3c9e5b7d1f4
Revises: f6b2d8a4c1e7
Create Date: 2026-10-08 12:00:00.000000

"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

# revision identifiers, used by Alembic.
revision: str = "a3c9e5b7d1f4"  # pragma: allowlist secret
down_revision: str | Sequence[str] | None = "f6b2d8a4c1e7"  # pragma: allowlist secret
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "gmail_message_index",
        sa.Column("id", sa.Integer(), nullable=False),
        sa.Column("owner_id", sa.Integer(), nullable=False),
        sa.Column("gmail_id", sa.String(), nullable=False),
        sa.Column("thread_id", sa.String(), nullable=False),
        sa.Column("message_id", sa.String(), nullable=True),
        sa.Column("sender", sa.String(), nullable=True),
        sa.Column("subject", sa.String(), nullable=True),
        sa.Column("sent_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("internal_id", sa.String(), nullable=True),
        sa.Column("az_court", sa.String(), nullable=True),
        sa.Column("group_key", sa.String(), nullable=True),
        sa.Column("group_kind", sa.String(), nullable=True),
        sa.Column(
            "has_attachments", sa.Boolean(), nullable=False, server_default=sa.false()
        ),
        sa.Column("size_estimate", sa.Integer(), nullable=True),
        sa.Column("indexed_at", sa.DateTime(timezone=True), nullable=False),
        sa.ForeignKeyConstraint(["owner_id"], ["users.id"], ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("owner_id", "gmail_id", name="uq_gmail_index_owner_gmail"),
    )
    op.create_index(
        "ix_gmail_index_owner_sent", "gmail_message_index", ["owner_id", "sent_at"]
    )
    op.create_index(
        "ix_gmail_index_owner_group", "gmail_message_index", ["owner_id", "group_key"]
    )


def downgrade() -> None:
    op.drop_index("ix_gmail_index_owner_group", table_name="gmail_message_index")
    op.drop_index("ix_gmail_index_owner_sent", table_name="gmail_message_index")
    op.drop_table("gmail_message_index")
