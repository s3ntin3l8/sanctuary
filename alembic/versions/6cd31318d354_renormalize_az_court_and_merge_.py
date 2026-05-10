"""renormalize_az_court_and_merge_proceedings

Revision ID: 6cd31318d354
Revises: e5b7c1d9f3a2
Create Date: 2026-05-10 14:21:45.174745

"""

import re
from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "6cd31318d354"
down_revision: str | Sequence[str] | None = "e5b7c1d9f3a2"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


# Inline copy of the new normalize_az_court() — must not import app code in migrations.
_AZ_CANONICAL_RE = re.compile(r"^\d+\s[A-Z]{1,3}\s\d+/\d+(?:\s[A-Z]{1,3})?$")


def _normalize(value: str | None) -> str | None:
    if not value:
        return None
    cleaned = re.sub(r"\s*\([^)]*\)\s*", "", value)
    cleaned = re.sub(r"(\d)\s*-\s*([A-Za-z])", r"\1 \2", cleaned)
    cleaned = re.sub(r"([A-Za-z])\s*-\s*(\d)", r"\1 \2", cleaned)
    cleaned = re.sub(r"(\d)([A-Za-z])", r"\1 \2", cleaned)
    cleaned = re.sub(r"([A-Za-z])(\d)", r"\1 \2", cleaned)
    cleaned = re.sub(r"\s+", " ", cleaned.strip())
    cleaned = re.sub(r"\s*/\s*", "/", cleaned)
    result = cleaned.upper()
    result = re.sub(r"^0+(\d)", r"\1", result)
    if not _AZ_CANONICAL_RE.match(result):
        return None
    return result


def upgrade() -> None:
    conn = op.get_bind()

    # 1. Re-normalize all Proceeding.az_court values.
    #    Values that don't pass the new validator are set to NULL
    #    (they were garbage AZs that created junk proceedings).
    rows = conn.execute(
        sa.text("SELECT id, az_court FROM proceedings WHERE az_court IS NOT NULL")
    ).fetchall()
    for pid, az in rows:
        new_az = _normalize(az)
        if new_az != az:
            conn.execute(
                sa.text("UPDATE proceedings SET az_court = :az WHERE id = :id"),
                {"az": new_az, "id": pid},
            )

    # 2. Re-normalize Document.az_court hint field (informational only, no FKs).
    doc_rows = conn.execute(
        sa.text("SELECT id, az_court FROM documents WHERE az_court IS NOT NULL")
    ).fetchall()
    for did, az in doc_rows:
        new_az = _normalize(az)
        if new_az != az:
            conn.execute(
                sa.text("UPDATE documents SET az_court = :az WHERE id = :id"),
                {"az": new_az, "id": did},
            )

    # 3. Merge duplicate (case_id, az_court) proceedings — same pattern as ca45aaa94f30.
    #    Keep the oldest (lowest id = MIN), repoint FKs, delete duplicates.
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

    # 4. Proceedings whose AZ was invalidated (now NULL) that have no linked documents
    #    or batches are orphaned junk — delete them.
    conn.execute(
        sa.text("""
            DELETE FROM proceedings
            WHERE az_court IS NULL
              AND court_name IN ('Unknown Court', 'General')
              AND id NOT IN (
                  SELECT DISTINCT proceeding_id FROM documents
                  WHERE proceeding_id IS NOT NULL
              )
              AND id NOT IN (
                  SELECT DISTINCT proceeding_id FROM ingest_batches
                  WHERE proceeding_id IS NOT NULL
              )
        """)
    )

    # 5. Mark all remaining AI-created proceedings as is_draft=1.
    #    Pre-release: all AI-detected proceedings require explicit user confirmation.
    conn.execute(
        sa.text("UPDATE proceedings SET is_draft = 1 WHERE az_court IS NOT NULL")
    )


def downgrade() -> None:
    pass  # data migration — irreversible (pre-release per CLAUDE.md)
