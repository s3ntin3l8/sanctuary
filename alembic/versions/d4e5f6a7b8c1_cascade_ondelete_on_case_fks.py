"""cascade_ondelete_on_case_fks

Revision ID: d4e5f6a7b8c1
Revises: c3d4e5f6a7b9
Create Date: 2026-04-29 00:00:00.000000

Add ondelete clauses to every Case-owning FK so SQL-level cascade matches
the service-level intent in `CaseService.delete_and_revert`. Deferred from
Wave 3a — landing it now together with chain FKs on `ClaimEvidence`.

Choices per table:
- Proceeding.case_id           NOT NULL  → CASCADE
- IngestBatch.case_id          NULL OK   → SET NULL
- ActionItem.case_id           NOT NULL  → CASCADE
- Claim.case_id                NOT NULL  → CASCADE
- LegalCost.case_id            NOT NULL  → CASCADE
- Entity.case_id               NOT NULL  → CASCADE
- ClaimEvidence.claim_id       NOT NULL  → CASCADE
- ClaimEvidence.document_id    NOT NULL  → CASCADE

Implementation: SQLite cannot ALTER an existing FK constraint. We use the
SQLite-recommended "create new, copy, drop old, rename" pattern. The new table
SQL is derived from sqlite_master (the ACTUAL live schema, not the ORM model),
with FK ON DELETE clauses patched via regex. This avoids the model-ahead bug
where Base.metadata may already include columns added by later migrations,
causing INSERT … SELECT to fail with "no such column".

The DROP TABLE IF EXISTS at the start of each table's processing makes the
migration idempotent: a leftover _alembic_batch_* zombie from a prior failed
run is cleaned up automatically on retry.
"""

import re
from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "d4e5f6a7b8c1"
down_revision: str | Sequence[str] | None = "c3d4e5f6a7b9"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


# Maps each table to the FK column(s) that need an ON DELETE clause added.
_FK_ONDELETE: dict[str, dict[str, str]] = {
    "proceedings": {"case_id": "CASCADE"},
    "ingest_batches": {"case_id": "SET NULL"},
    "action_items": {"case_id": "CASCADE"},
    "claims": {"case_id": "CASCADE"},
    "claim_evidence": {"claim_id": "CASCADE", "document_id": "CASCADE"},
    "legal_costs": {"case_id": "CASCADE"},
    "entities": {"case_id": "CASCADE"},
}


def _patch_ondelete(sql: str, col_ondelete: dict[str, str]) -> str:
    """Add or replace ON DELETE clauses on specific FK columns in a CREATE TABLE SQL."""
    for col, action in col_ondelete.items():
        sql = re.sub(
            rf"(FOREIGN KEY\s*\(\s*{re.escape(col)}\s*\)\s*REFERENCES\s+\w+\s*\(\s*\w+\s*\))"
            r"(?:\s+ON DELETE \w+)?",
            rf"\1 ON DELETE {action}",
            sql,
        )
    return sql


def _recreate_with_new_fks(table_name: str, col_ondelete: dict[str, str]) -> None:
    tmp = f"_alembic_batch_{table_name}"
    conn = op.get_bind()

    # Idempotency: drop any zombie temp table left by a previous failed run.
    op.execute(sa.text(f"DROP TABLE IF EXISTS {tmp}"))

    # Source the ACTUAL current schema from sqlite_master, not from Base.metadata.
    # Using the model here would include columns added by later migrations that are
    # not yet in the live table, causing the INSERT … SELECT to fail.
    row = conn.execute(
        sa.text("SELECT sql FROM sqlite_master WHERE type='table' AND name=:n"),
        {"n": table_name},
    ).fetchone()
    original_sql: str = row[0]

    # Patch the FK ON DELETE clauses and rename for the temp table.
    # sqlite_master may store the table name with or without double-quotes
    # depending on how the table was originally created, so handle both.
    new_sql = _patch_ondelete(original_sql, col_ondelete)
    new_sql = re.sub(
        rf'CREATE\s+TABLE\s+(?:"{re.escape(table_name)}"|{re.escape(table_name)})',
        f"CREATE TABLE {tmp}",
        new_sql,
        count=1,
    )

    # Column list from PRAGMA — guaranteed to match the source table exactly.
    # Read BEFORE the DROP below; PRAGMA returns nothing once the table is gone.
    db_cols = [
        r[1]
        for r in conn.execute(sa.text(f"PRAGMA table_info({table_name})")).fetchall()
    ]
    cols_csv = ", ".join(db_cols)

    # Collect index SQL BEFORE DROP TABLE — DROP TABLE wipes them from sqlite_master.
    index_sqls = [
        r[0]
        for r in conn.execute(
            sa.text(
                "SELECT sql FROM sqlite_master"
                " WHERE type='index' AND tbl_name=:n AND sql IS NOT NULL"
            ),
            {"n": table_name},
        ).fetchall()
    ]

    op.execute(sa.text(new_sql))
    op.execute(
        sa.text(f"INSERT INTO {tmp} ({cols_csv}) SELECT {cols_csv} FROM {table_name}")
    )
    op.execute(sa.text(f"DROP TABLE {table_name}"))
    op.execute(sa.text(f"ALTER TABLE {tmp} RENAME TO {table_name}"))

    # Re-create indexes on the newly renamed table.
    for idx_sql in index_sqls:
        op.execute(sa.text(idx_sql))


def upgrade() -> None:
    op.execute(sa.text("PRAGMA foreign_keys=OFF"))
    try:
        for table_name, col_ondelete in _FK_ONDELETE.items():
            _recreate_with_new_fks(table_name, col_ondelete)
    finally:
        op.execute(sa.text("PRAGMA foreign_keys=ON"))


def downgrade() -> None:
    raise NotImplementedError("Downgrade not supported")
