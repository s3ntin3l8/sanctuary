"""drop the audit rows of the removed "redact AI debug logs" setting

The redact toggle is gone (full prompt capture is now an opt-in switch, and what
it records is encrypted), so its audit rows name an event that no longer exists
and could not be loaded.

Revision ID: a7c3e9f1b5d2
Revises: d4b8f2a6c1e9
Create Date: 2026-10-18 09:00:00.000000

"""

from collections.abc import Sequence

from alembic import op

# revision identifiers, used by Alembic.
revision: str = "a7c3e9f1b5d2"  # pragma: allowlist secret
down_revision: str | Sequence[str] | None = "d4b8f2a6c1e9"  # pragma: allowlist secret
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.execute("DELETE FROM audit_logs WHERE event_type = 'AI_DEBUG_REDACT_TOGGLED'")


def downgrade() -> None:
    pass  # the deleted rows describe a setting that no longer exists
