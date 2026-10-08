"""gmail sync mode (off/notify/auto) replaces the auto-sync flag; index gains received_at

Revision ID: b8d2f4a6c0e1
Revises: a3c9e5b7d1f4
Create Date: 2026-10-08 18:00:00.000000

Data migration: ``gmail_auto_sync`` (bool) becomes ``gmail_sync_mode`` for every
user. A connected user who had automatic sync on keeps it ("auto"); everyone else
moves to "notify" (check for new mail, import only after confirmation). Users with
no connection just lose the old flag. Downgrade maps back: auto -> True, else False.

``received_at`` is Gmail's receipt time of an indexed message, so "new since the
sync point" doesn't depend on the (sender-controlled) Date header. Rows indexed
before this migration have none and fall back to ``sent_at``.
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

# revision identifiers, used by Alembic.
revision: str = "b8d2f4a6c0e1"  # pragma: allowlist secret
down_revision: str | Sequence[str] | None = "a3c9e5b7d1f4"  # pragma: allowlist secret
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def _rewrite(transform) -> None:
    bind = op.get_bind()
    table = sa.table(
        "user_settings",
        sa.column("id", sa.Integer),
        sa.column("settings_json", postgresql.JSONB(astext_type=sa.Text())),
    )
    for row_id, data in bind.execute(
        sa.select(table.c.id, table.c.settings_json)
    ).fetchall():
        if not isinstance(data, dict):
            continue
        updated = transform(data)
        if updated is not None:
            bind.execute(
                table.update().where(table.c.id == row_id).values(settings_json=updated)
            )


def _to_mode(data: dict) -> dict | None:
    if "gmail_auto_sync" not in data and not data.get("gmail_credentials_json"):
        return None
    new = {k: v for k, v in data.items() if k != "gmail_auto_sync"}
    if data.get("gmail_credentials_json"):
        new["gmail_sync_mode"] = "auto" if data.get("gmail_auto_sync") else "notify"
    return new


def _to_flag(data: dict) -> dict | None:
    if "gmail_sync_mode" not in data:
        return None
    new = {k: v for k, v in data.items() if k != "gmail_sync_mode"}
    new["gmail_auto_sync"] = data["gmail_sync_mode"] == "auto"
    return new


def upgrade() -> None:
    op.add_column(
        "gmail_message_index", sa.Column("received_at", sa.DateTime(), nullable=True)
    )
    _rewrite(_to_mode)


def downgrade() -> None:
    _rewrite(_to_flag)
    op.drop_column("gmail_message_index", "received_at")
