"""phase7_chat_conversations

Revision ID: 8ef2d25dee29
Revises: 833f6caa97c7
Create Date: 2026-04-22 18:39:09.511617

"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

# revision identifiers, used by Alembic.
revision: str = "8ef2d25dee29"
down_revision: str | Sequence[str] | None = "833f6caa97c7"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    """Upgrade schema."""
    op.create_table(
        "conversations",
        sa.Column("id", sa.Integer(), nullable=False),
        sa.Column("scope_type", sa.String(), nullable=False),
        sa.Column("scope_id", sa.String(), nullable=False),
        sa.Column("title", sa.String(), nullable=True),
        sa.Column("created_at", sa.DateTime(), nullable=False),
        sa.PrimaryKeyConstraint("id"),
    )
    with op.batch_alter_table("conversations", schema=None) as batch_op:
        batch_op.create_index("ix_conversations_id", ["id"], unique=False)
        batch_op.create_index(
            "ix_conversations_scope", ["scope_type", "scope_id"], unique=False
        )

    op.create_table(
        "conversation_messages",
        sa.Column("id", sa.Integer(), nullable=False),
        sa.Column("conversation_id", sa.Integer(), nullable=False),
        sa.Column("role", sa.String(), nullable=False),
        sa.Column("content", sa.Text(), nullable=False),
        sa.Column("context_document_ids", sa.JSON(), nullable=True),
        sa.Column("created_at", sa.DateTime(), nullable=False),
        sa.ForeignKeyConstraint(["conversation_id"], ["conversations.id"]),
        sa.PrimaryKeyConstraint("id"),
    )
    with op.batch_alter_table("conversation_messages", schema=None) as batch_op:
        batch_op.create_index("ix_conversation_messages_id", ["id"], unique=False)
        batch_op.create_index(
            "ix_conversation_messages_conversation", ["conversation_id"], unique=False
        )


def downgrade() -> None:
    """Downgrade schema."""
    with op.batch_alter_table("conversation_messages", schema=None) as batch_op:
        batch_op.drop_index("ix_conversation_messages_conversation")
        batch_op.drop_index("ix_conversation_messages_id")
    op.drop_table("conversation_messages")

    with op.batch_alter_table("conversations", schema=None) as batch_op:
        batch_op.drop_index("ix_conversations_scope")
        batch_op.drop_index("ix_conversations_id")
    op.drop_table("conversations")
