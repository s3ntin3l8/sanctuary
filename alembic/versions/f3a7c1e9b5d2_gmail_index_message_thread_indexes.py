"""index gmail_message_index by (owner, message_id) and (owner, thread_id)

The Gmail-thread fallback of thread_header_linker joins ingest_batches to the
index on Message-ID and looks up a thread's members; the history-import page's
ingested-ness check does the same join. Both were unindexed scans.

Revision ID: f3a7c1e9b5d2
Revises: e1b5d9a3c7f4
Create Date: 2026-10-08 19:30:00.000000

"""

from collections.abc import Sequence

from alembic import op

# revision identifiers, used by Alembic.
revision: str = "f3a7c1e9b5d2"  # pragma: allowlist secret
down_revision: str | Sequence[str] | None = "e1b5d9a3c7f4"  # pragma: allowlist secret
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_index(
        "ix_gmail_index_owner_message",
        "gmail_message_index",
        ["owner_id", "message_id"],
    )
    op.create_index(
        "ix_gmail_index_owner_thread",
        "gmail_message_index",
        ["owner_id", "thread_id"],
    )


def downgrade() -> None:
    op.drop_index("ix_gmail_index_owner_thread", table_name="gmail_message_index")
    op.drop_index("ix_gmail_index_owner_message", table_name="gmail_message_index")
