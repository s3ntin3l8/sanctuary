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

Implementation: SQLite cannot ALTER an existing FK constraint, and Alembic's
`batch_alter_table(copy_from=..., recreate="always")` does not propagate the
new FK definitions from the model into the regenerated CREATE TABLE — it uses
the autoloaded structure for constraints. So we use the SQLite-recommended
"create new, copy, drop old, rename" pattern manually, sourcing the new
table SQL from SQLAlchemy's `CreateTable` compiled against the model's
metadata (which carries the new ondelete clauses).
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import sqlite as sqlite_dialect
from sqlalchemy.schema import CreateIndex, CreateTable

from app.models.database import Base

revision: str = "d4e5f6a7b8c1"
down_revision: str | Sequence[str] | None = "c3d4e5f6a7b9"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


_TABLES = [
    "proceedings",
    "ingest_batches",
    "action_items",
    "claims",
    "claim_evidence",
    "legal_costs",
    "entities",
]


def _column_names(table: sa.Table) -> list[str]:
    return [c.name for c in table.columns]


def _recreate_with_new_fks(table_name: str) -> None:
    table = Base.metadata.tables[table_name]
    cols = _column_names(table)
    cols_csv = ", ".join(cols)

    new_table_sql = str(
        CreateTable(table).compile(dialect=sqlite_dialect.dialect())
    ).strip()
    # Rename the model's table to a temp name in the SQL so we can keep the
    # original name for the live table during the copy.
    tmp_table_name = f"_alembic_batch_{table_name}"
    new_table_sql_renamed = new_table_sql.replace(
        f"CREATE TABLE {table_name} (", f"CREATE TABLE {tmp_table_name} ("
    )

    op.execute(new_table_sql_renamed)
    op.execute(
        f"INSERT INTO {tmp_table_name} ({cols_csv}) SELECT {cols_csv} FROM {table_name}"
    )
    op.execute(f"DROP TABLE {table_name}")
    op.execute(f"ALTER TABLE {tmp_table_name} RENAME TO {table_name}")

    # Re-create indexes from the model. DROP TABLE wiped them along with
    # the old table, so we need to emit them fresh.
    for index in table.indexes:
        op.execute(str(CreateIndex(index).compile(dialect=sqlite_dialect.dialect())))


def upgrade() -> None:
    op.execute("PRAGMA foreign_keys=OFF")
    try:
        for table_name in _TABLES:
            _recreate_with_new_fks(table_name)
    finally:
        op.execute("PRAGMA foreign_keys=ON")


def downgrade() -> None:
    raise NotImplementedError("Downgrade not supported")
