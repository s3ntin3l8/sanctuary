"""encrypt stored credentials (Gmail OAuth token, AI endpoint API keys)

Revision ID: f6b2d8a4c1e7
Revises: d4e1f2a3b5c6
Create Date: 2026-10-08 09:00:00.000000

Data-only migration: the settings JSON blobs held these as plaintext. Only rows
that actually carry a secret are touched, so a fresh database needs no
SECRETS_ENCRYPTION_KEY to migrate.
"""

from collections.abc import Callable, Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

from app.core import secrets

# revision identifiers, used by Alembic.
revision: str = "f6b2d8a4c1e7"  # pragma: allowlist secret
down_revision: str | Sequence[str] | None = "d4e1f2a3b5c6"  # pragma: allowlist secret
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

_NO_KEY = "not-needed"  # AI placeholder for keyless endpoints; not a secret


def _user_settings(data: dict, fn: Callable[[str], str]) -> dict | None:
    value = data.get("gmail_credentials_json")
    if not value:
        return None
    new = fn(value)
    return None if new == value else {**data, "gmail_credentials_json": new}


def _app_settings(data: dict, fn: Callable[[str], str]) -> dict | None:
    ai = data.get("ai")
    if not isinstance(ai, dict) or not ai.get("instances"):
        return None
    changed = False
    instances = []
    for inst in ai["instances"]:
        key = inst.get("api_key")
        if key and key != _NO_KEY:
            new = fn(key)
            if new != key:
                inst = {**inst, "api_key": new}
                changed = True
        instances.append(inst)
    return {**data, "ai": {**ai, "instances": instances}} if changed else None


def _rewrite(fn: Callable[[str], str]) -> None:
    bind = op.get_bind()
    for table_name, transform in (
        ("user_settings", _user_settings),
        ("app_settings", _app_settings),
    ):
        table = sa.table(
            table_name,
            sa.column("id", sa.Integer),
            sa.column("settings_json", postgresql.JSONB(astext_type=sa.Text())),
        )
        for row_id, data in bind.execute(
            sa.select(table.c.id, table.c.settings_json)
        ).fetchall():
            if not isinstance(data, dict):
                continue
            updated = transform(data, fn)
            if updated is not None:
                bind.execute(
                    table.update()
                    .where(table.c.id == row_id)
                    .values(settings_json=updated)
                )


def upgrade() -> None:
    _rewrite(secrets.encrypt)


def downgrade() -> None:
    _rewrite(secrets.decrypt)
