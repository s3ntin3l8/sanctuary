"""phase1: chat (Conversation, ConversationMessage)

Revision ID: 47e3c9c59e84
Revises: 404c6c87d3f1
Create Date: 2026-04-16 00:00:02.000000

Phase 1 — Migration C. Purely additive.

* New tables:
    - conversations (chat thread scoped to case or document)
    - conversation_messages (individual user/assistant messages, with source
      document ids used as grounding context)
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "47e3c9c59e84"
down_revision: str | Sequence[str] | None = "404c6c87d3f1"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "conversations",
        sa.Column("id", sa.Integer(), nullable=False),
        sa.Column("scope_type", sa.String(), nullable=False),
        sa.Column("scope_id", sa.String(), nullable=False),
        sa.Column("title", sa.String(), nullable=True),
        sa.Column("created_at", sa.DateTime(), nullable=False),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index("ix_conversations_id", "conversations", ["id"])
    op.create_index(
        "ix_conversations_scope", "conversations", ["scope_type", "scope_id"]
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
    op.create_index("ix_conversation_messages_id", "conversation_messages", ["id"])
    op.create_index(
        "ix_conversation_messages_conversation",
        "conversation_messages",
        ["conversation_id"],
    )


def downgrade() -> None:
    op.drop_index(
        "ix_conversation_messages_conversation",
        table_name="conversation_messages",
    )
    op.drop_index("ix_conversation_messages_id", table_name="conversation_messages")
    op.drop_table("conversation_messages")

    op.drop_index("ix_conversations_scope", table_name="conversations")
    op.drop_index("ix_conversations_id", table_name="conversations")
    op.drop_table("conversations")
