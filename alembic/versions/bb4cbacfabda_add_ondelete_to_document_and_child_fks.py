"""add_ondelete_to_document_and_child_fks

Revision ID: bb4cbacfabda
Revises: 697d2604431a
Create Date: 2026-05-17 00:00:00.000000

Add ondelete clauses to the remaining FKs across Document, IngestBatch, and
their child tables so SQL-level cascade matches service-level intent. This is
a follow-up to ``d4e5f6a7b8c1_cascade_ondelete_on_case_fks`` which covered
case-owning FKs; the same SQLite "create new, copy, drop old, rename" pattern
is used here because SQLite cannot ALTER an existing FK constraint and
``batch_alter_table`` does not propagate new FK definitions from the model
metadata into the regenerated CREATE TABLE.

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

from collections.abc import Sequence

import sqlalchemy as sa  # noqa: F401
from alembic import op
from sqlalchemy.dialects import sqlite as sqlite_dialect
from sqlalchemy.schema import CreateIndex, CreateTable

from app.models.database import Base

revision: str = "bb4cbacfabda"
down_revision: str | Sequence[str] | None = "697d2604431a"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


_TABLES = [
    "documents",
    "ingest_batches",
    "document_relationships",
    "action_items",
    "legal_costs",
    "conversation_messages",
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
