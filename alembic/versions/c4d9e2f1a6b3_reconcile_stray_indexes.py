"""reconcile stray indexes with the model

Revision ID: c4d9e2f1a6b3
Revises: 68953086cfe8
Create Date: 2026-05-30 00:00:00.000000

Bring the live index set in line with Base.metadata so `alembic check` is
clean. Two leftovers accumulated across the messy index history (the same
old-name vs. op.f()-name split that bit `claims` and forced the
d2c4f9a1b6e8 fix):

* ``ix_action_items_source_document`` — created by an early migration as an
  explicit single-column index, but the model now indexes
  ``source_document_id`` only via ``index=True`` (``ix_action_items_source_document_id``,
  still present). The non-``_id`` name is a stray the model never declares.

* ``ix_conversation_messages_conversation_id`` — the model declares this via
  ``index=True`` on ``conversation_id`` (alongside the explicit
  ``ix_conversation_messages_conversation``), but the conversation-tables
  recreate in 8ef2d25dee29 only emitted the explicit name, so the live DB was
  missing the ``_id`` one.

IF EXISTS / IF NOT EXISTS guards keep this idempotent across databases that
took different historical paths (fresh, the dev DB, etc.).
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "c4d9e2f1a6b3"
down_revision: str | Sequence[str] | None = "68953086cfe8"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.execute(sa.text("DROP INDEX IF EXISTS ix_action_items_source_document"))
    op.execute(
        sa.text(
            "CREATE INDEX IF NOT EXISTS ix_conversation_messages_conversation_id "
            "ON conversation_messages (conversation_id)"
        )
    )


def downgrade() -> None:
    op.execute(sa.text("DROP INDEX IF EXISTS ix_conversation_messages_conversation_id"))
    op.execute(
        sa.text(
            "CREATE INDEX IF NOT EXISTS ix_action_items_source_document "
            "ON action_items (source_document_id)"
        )
    )
