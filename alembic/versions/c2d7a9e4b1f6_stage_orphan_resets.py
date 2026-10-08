"""count orphan resets per pipeline stage

Lets the orphan sweeper stop resetting a stage that keeps blowing the Celery
time limit (a poison document) and fail it instead.

Revision ID: c2d7a9e4b1f6
Revises: b8d2f4a6c0e1
Create Date: 2026-10-08 12:00:00.000000

"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

# revision identifiers, used by Alembic.
revision: str = "c2d7a9e4b1f6"  # pragma: allowlist secret
down_revision: str | Sequence[str] | None = "b8d2f4a6c0e1"  # pragma: allowlist secret
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.add_column(
        "document_pipeline_stages",
        sa.Column("orphan_resets", sa.Integer(), server_default="0", nullable=False),
    )


def downgrade() -> None:
    op.drop_column("document_pipeline_stages", "orphan_resets")
