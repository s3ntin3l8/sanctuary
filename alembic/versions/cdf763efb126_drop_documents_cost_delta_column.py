"""drop_documents_cost_delta_column

Revision ID: cdf763efb126
Revises: f6bff048288b
Create Date: 2026-05-18 08:01:22.022431

Phase 3 of the cost_delta → CostSignal migration. By this point all readers
and writers go through CostSignal (orphan kinds) or LegalCost (invoice/
vorschuss). The JSON column has no live consumers.

Pattern mirrors 03d0a6ae6981_drop_documents_pipeline_stages_column.py.
Irreversible — Phase 1's backfill is the data migration path forward.
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

# revision identifiers, used by Alembic.
revision: str = "cdf763efb126"
down_revision: str | Sequence[str] | None = "f6bff048288b"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    # Guard: column may be absent if removed by a prior migration or schema reset.
    conn = op.get_bind()
    col_names = {
        r[1] for r in conn.execute(sa.text("PRAGMA table_info(documents)")).fetchall()
    }
    if "cost_delta" not in col_names:
        return

    with op.batch_alter_table("documents") as batch_op:
        batch_op.drop_column("cost_delta")


def downgrade() -> None:
    raise NotImplementedError(
        "data migration; irreversible — use the f6bff048288b backfill"
    )
