"""drop_documents_pipeline_stages_column

Revision ID: 03d0a6ae6981
Revises: 05a267c5420a
Create Date: 2026-05-18 01:02:09.483293

"""

from collections.abc import Sequence

from alembic import op

# revision identifiers, used by Alembic.
revision: str = "03d0a6ae6981"
down_revision: str | Sequence[str] | None = "05a267c5420a"
branch_labels = None
depends_on = None


def upgrade() -> None:
    with op.batch_alter_table("documents") as batch_op:
        batch_op.drop_column("pipeline_stages")


def downgrade() -> None:
    raise NotImplementedError("data migration; irreversible — use Phase 8.1 backfill")
