"""move case-brief job state out of the ai_brief JSON

`Case.ai_brief` doubled as the job-state carrier ({"status": "processing"} /
{"status": "failed"}), so starting a refresh or failing one destroyed the last
good brief. Job state now lives in brief_state / brief_error and ai_brief keeps
only the last successfully generated brief.

One-way for the job state: processing/failed are transient breadcrumbs (a
refresh re-sets them), so downgrade drops the columns without restoring the
old {"status": ...} JSON.

Revision ID: a7c3e9d1b5f2
Revises: f3a7c1e9b5d2
Create Date: 2026-10-08 21:00:00.000000

"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

# revision identifiers, used by Alembic.
revision: str = "a7c3e9d1b5f2"  # pragma: allowlist secret
down_revision: str | Sequence[str] | None = "f3a7c1e9b5d2"  # pragma: allowlist secret
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.add_column(
        "cases",
        sa.Column(
            "brief_state",
            sa.Enum(
                "IDLE",
                "PROCESSING",
                "FAILED",
                name="briefstate",
                native_enum=False,
            ),
            nullable=False,
            server_default="IDLE",
        ),
    )
    op.add_column("cases", sa.Column("brief_error", sa.Text(), nullable=True))
    # Names, not values: SQLAlchemy stores the enum member name.
    op.execute(
        """
        UPDATE cases
        SET brief_state = CASE ai_brief->>'status'
                WHEN 'processing' THEN 'PROCESSING' ELSE 'FAILED' END,
            brief_error = ai_brief->>'error',
            ai_brief = NULL
        WHERE ai_brief->>'status' IN ('processing', 'failed')
        """
    )
    op.alter_column("cases", "brief_state", server_default=None)


def downgrade() -> None:
    # Transient job state is not restored into ai_brief (see module docstring).
    op.drop_column("cases", "brief_error")
    op.drop_column("cases", "brief_state")
