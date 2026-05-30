"""add_ondelete_to_document_and_child_fks

Revision ID: bb4cbacfabda
Revises: 697d2604431a
Create Date: 2026-05-17 00:00:00.000000

Add ondelete clauses to the remaining FKs across Document, IngestBatch, and
their child tables so SQL-level cascade matches service-level intent. This is
a follow-up to ``d4e5f6a7b8c1_cascade_ondelete_on_case_fks`` which covered
case-owning FKs; the same SQLite "create new, copy, drop old, rename" pattern
is used here because SQLite cannot ALTER an existing FK constraint.

The new table SQL is derived from sqlite_master (the ACTUAL live schema, not
the ORM model), with the target FK ON DELETE clauses patched via regex. This
mirrors ``d4e5f6a7b8c1`` and avoids the model-ahead bug: deriving the CREATE
TABLE from ``Base.metadata`` would include columns added by *later* migrations
(e.g. ``ingest_batches.attachment_manifest``/``email_note`` from 68953086cfe8)
that are not yet present in the live table, making INSERT … SELECT fail on a
fresh database with "no such column".

Choices per FK:
- Document.parent_id                       NULL OK   → SET NULL
- Document.ingest_batch_id                 NULL OK   → SET NULL
- Document.proceeding_id                   NULL OK   → SET NULL
- IngestBatch.proceeding_id                NULL OK   → SET NULL
- DocumentRelationship.from_document_id    NOT NULL  → CASCADE
- DocumentRelationship.to_document_id      NOT NULL  → CASCADE
- ActionItem.proceeding_id                 NULL OK   → SET NULL
- ActionItem.source_document_id            NULL OK   → SET NULL
- LegalCost.source_document_id             NULL OK   → SET NULL
- LegalCost.offsets_cost_id                NULL OK   → SET NULL
- ConversationMessage.conversation_id      NOT NULL  → CASCADE
- Entity.source_document_id                NULL OK   → SET NULL
"""

import re
from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "bb4cbacfabda"
down_revision: str | Sequence[str] | None = "697d2604431a"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


# Maps each table to the FK column(s) that need an ON DELETE clause added.
# Case-owning FKs were already handled by d4e5f6a7b8c1; these are the rest.
_FK_ONDELETE: dict[str, dict[str, str]] = {
    "documents": {
        "parent_id": "SET NULL",
        "ingest_batch_id": "SET NULL",
        "proceeding_id": "SET NULL",
    },
    "ingest_batches": {"proceeding_id": "SET NULL"},
    "document_relationships": {
        "from_document_id": "CASCADE",
        "to_document_id": "CASCADE",
    },
    "action_items": {
        "proceeding_id": "SET NULL",
        "source_document_id": "SET NULL",
    },
    "legal_costs": {
        "source_document_id": "SET NULL",
        "offsets_cost_id": "SET NULL",
    },
    "conversation_messages": {"conversation_id": "CASCADE"},
    "entities": {"source_document_id": "SET NULL"},
}


def _patch_ondelete(sql: str, col_ondelete: dict[str, str]) -> str:
    """Add or replace ON DELETE clauses on specific FK columns in a CREATE TABLE SQL."""
    for col, action in col_ondelete.items():
        new_sql, n = re.subn(
            rf"(FOREIGN KEY\s*\(\s*{re.escape(col)}\s*\)\s*REFERENCES\s+\w+\s*\(\s*\w+\s*\))"
            r"(?:\s+ON DELETE \w+)?",
            rf"\1 ON DELETE {action}",
            sql,
        )
        if n == 0:
            # Loud failure beats a silently un-patched FK: if the regex stops
            # matching the live schema, the ON DELETE clause would just be
            # dropped and `upgrade head` would still go green with the wrong
            # schema. Fail instead so the mismatch is caught.
            raise RuntimeError(
                f"Could not locate FOREIGN KEY({col}) clause to patch; "
                f"live CREATE TABLE SQL does not match the expected format."
            )
        sql = new_sql
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
