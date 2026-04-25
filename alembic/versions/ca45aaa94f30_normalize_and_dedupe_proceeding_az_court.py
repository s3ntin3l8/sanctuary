"""normalize_and_dedupe_proceeding_az_court

Revision ID: ca45aaa94f30
Revises: fff7ef1713b7
Create Date: 2026-04-25 14:03:00.212704

"""

import re
from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

# revision identifiers, used by Alembic.
revision: str = "ca45aaa94f30"
down_revision: str | Sequence[str] | None = "fff7ef1713b7"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    conn = op.get_bind()

    # 1. Normalize all existing az_court values (collapse whitespace, uppercase).
    rows = conn.execute(
        sa.text("SELECT id, az_court FROM proceedings WHERE az_court IS NOT NULL")
    ).fetchall()
    for pid, az in rows:
        norm = re.sub(r"\s+", " ", az.strip()).upper()
        if norm != az:
            conn.execute(
                sa.text("UPDATE proceedings SET az_court = :az WHERE id = :id"),
                {"az": norm, "id": pid},
            )

    # 2. Within each (case_id, az_court) group, keep the oldest proceeding,
    #    repoint FKs from duplicates to the keeper, then delete the duplicates.
    dupes = conn.execute(
        sa.text("""
            SELECT case_id, az_court, MIN(id) AS keeper, GROUP_CONCAT(id) AS all_ids
            FROM proceedings
            WHERE az_court IS NOT NULL
            GROUP BY case_id, az_court
            HAVING COUNT(*) > 1
        """)
    ).fetchall()
    for row in dupes:
        keeper = int(row[2])
        all_ids = [int(x) for x in row[3].split(",")]
        dup_ids = [x for x in all_ids if x != keeper]
        for dup_id in dup_ids:
            conn.execute(
                sa.text(
                    "UPDATE documents SET proceeding_id = :k WHERE proceeding_id = :d"
                ),
                {"k": keeper, "d": dup_id},
            )
            conn.execute(
                sa.text(
                    "UPDATE ingest_batches SET proceeding_id = :k WHERE proceeding_id = :d"
                ),
                {"k": keeper, "d": dup_id},
            )
            conn.execute(
                sa.text("DELETE FROM proceedings WHERE id = :d"),
                {"d": dup_id},
            )


def downgrade() -> None:
    pass  # data migration — irreversible (pre-release per CLAUDE.md)
