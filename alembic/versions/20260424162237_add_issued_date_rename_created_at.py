"""Add issued_date to documents, rename created_at to ingest_date.

Revision ID: 20260424162237
Revises: 030717acc3b8
Create Date: 2026-04-24 16:22:37

Note: issued_date and ingest_date columns already exist on some tables
(documents, proceedings, action_items, ingest_batches, document_relationships, claims).
This migration applies only the changes not yet present in the database.

"""

import sqlalchemy as sa
from alembic import op

revision = "20260424162237"
down_revision = "030717acc3b8"
branch_labels = None
depends_on = None


def upgrade() -> None:
    conn = op.get_bind()

    doc_cols = [
        r[1] for r in conn.execute(sa.text("PRAGMA table_info(documents)")).fetchall()
    ]
    if "issued_date" not in doc_cols:
        op.add_column(
            "documents",
            sa.Column("issued_date", sa.DateTime, nullable=True, index=True),
        )

    _tables_with_created_at = [
        ("documents", "created_at"),
        ("cases", "created_at"),
        ("legal_costs", "created_at"),
        ("saved_searches", "created_at"),
        ("claim_evidence", "created_at"),
        ("user_reactions", "created_at"),
        ("document_pins", "created_at"),
        ("entities", "created_at"),
        ("conversations", "created_at"),
        ("conversation_messages", "created_at"),
    ]
    for table, old_col in _tables_with_created_at:
        cols = [
            r[1]
            for r in conn.execute(sa.text(f"PRAGMA table_info({table})")).fetchall()
        ]
        if old_col in cols and "ingest_date" not in cols:
            op.alter_column(table, old_col, new_column_name="ingest_date")


def downgrade() -> None:
    conn = op.get_bind()

    _tables_with_ingest_date = [
        ("documents", "ingest_date"),
        ("cases", "ingest_date"),
        ("legal_costs", "ingest_date"),
        ("saved_searches", "ingest_date"),
        ("claim_evidence", "ingest_date"),
        ("user_reactions", "ingest_date"),
        ("document_pins", "ingest_date"),
        ("entities", "ingest_date"),
        ("conversations", "ingest_date"),
        ("conversation_messages", "ingest_date"),
    ]
    for table, old_col in _tables_with_ingest_date:
        cols = [
            r[1]
            for r in conn.execute(sa.text(f"PRAGMA table_info({table})")).fetchall()
        ]
        if old_col in cols and "created_at" not in cols:
            op.alter_column(table, old_col, new_column_name="created_at")

    doc_cols = [
        r[1] for r in conn.execute(sa.text("PRAGMA table_info(documents)")).fetchall()
    ]
    if "issued_date" in doc_cols:
        op.drop_column("documents", "issued_date")
