"""owner-scoped dedup uniqueness on ingest_batches

Revision ID: c7c7bfc79adf
Revises: b9fafd2f3daf
Create Date: 2026-09-28 08:05:13.291738

"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

# revision identifiers, used by Alembic.
revision: str = "c7c7bfc79adf"
down_revision: str | Sequence[str] | None = "b9fafd2f3daf"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def _check_no_duplicates(conn, column: str) -> None:
    """Abort with a readable list rather than let CREATE UNIQUE CONSTRAINT
    fail with a bare Postgres error, or silently delete data no one asked
    to lose. A UNIQUE(owner_id, col) constraint never treats a NULL as
    equal to anything else (standard SQL) — both columns must be non-NULL
    to collide, so the pre-check has to filter both the same way, or it
    would flag rows the constraint itself would happily allow."""
    rows = conn.execute(
        sa.text(
            f"""
            SELECT owner_id, {column}, COUNT(*) AS n
            FROM ingest_batches
            WHERE {column} IS NOT NULL AND owner_id IS NOT NULL
            GROUP BY owner_id, {column}
            HAVING COUNT(*) > 1
            """
        )
    ).fetchall()
    if rows:
        details = "\n".join(
            f"  owner_id={r.owner_id!r} {column}={getattr(r, column)!r} ({r.n} rows)"
            for r in rows
        )
        raise RuntimeError(
            f"Cannot add UNIQUE(owner_id, {column}) on ingest_batches — "
            f"existing duplicate rows found:\n{details}\n"
            "Resolve these (merge or delete the extra batches) before "
            "retrying this migration."
        )


def upgrade() -> None:
    """Upgrade schema."""
    conn = op.get_bind()
    _check_no_duplicates(conn, "message_id")
    _check_no_duplicates(conn, "source_hash")
    op.create_unique_constraint(
        "uq_ingest_batches_owner_message_id",
        "ingest_batches",
        ["owner_id", "message_id"],
    )
    op.create_unique_constraint(
        "uq_ingest_batches_owner_source_hash",
        "ingest_batches",
        ["owner_id", "source_hash"],
    )


def downgrade() -> None:
    """Downgrade schema."""
    op.drop_constraint(
        "uq_ingest_batches_owner_source_hash", "ingest_batches", type_="unique"
    )
    op.drop_constraint(
        "uq_ingest_batches_owner_message_id", "ingest_batches", type_="unique"
    )
