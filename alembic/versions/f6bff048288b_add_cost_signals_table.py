"""add_cost_signals_table

Revision ID: f6bff048288b
Revises: 03d0a6ae6981
Create Date: 2026-05-18 07:42:40.931447

Adds the `cost_signals` sibling table to `legal_costs` for cost-regime
metadata events (streitwert, cost_ruling, pkh_grant, pkh_denied) and
backfills it from the existing `documents.cost_delta` JSON column.

The `documents.cost_delta` column is NOT dropped here — that happens
in a follow-up migration once readers/writers are refactored.
"""

import json
from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

# revision identifiers, used by Alembic.
revision: str = "f6bff048288b"
down_revision: str | Sequence[str] | None = "03d0a6ae6981"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


_BACKFILL_KINDS = {"streitwert", "cost_ruling", "pkh_grant", "pkh_denied"}


def upgrade() -> None:
    op.create_table(
        "cost_signals",
        sa.Column("id", sa.Integer(), nullable=False),
        sa.Column("case_id", sa.String(), nullable=False),
        sa.Column("proceeding_id", sa.Integer(), nullable=True),
        sa.Column("source_document_id", sa.Integer(), nullable=False),
        sa.Column(
            "signal_type",
            sa.Enum(
                "streitwert",
                "cost_ruling",
                "pkh_grant",
                "pkh_denied",
                name="costsignaltype",
            ),
            nullable=False,
        ),
        sa.Column("amount", sa.Float(), nullable=True),
        sa.Column("allocation", sa.JSON(), nullable=True),
        sa.Column("description", sa.Text(), nullable=True),
        sa.Column("issued_at", sa.DateTime(), nullable=True),
        sa.Column("ingest_date", sa.DateTime(), nullable=False),
        sa.ForeignKeyConstraint(["case_id"], ["cases.id"], ondelete="CASCADE"),
        sa.ForeignKeyConstraint(
            ["proceeding_id"], ["proceedings.id"], ondelete="CASCADE"
        ),
        sa.ForeignKeyConstraint(
            ["source_document_id"], ["documents.id"], ondelete="CASCADE"
        ),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint(
            "source_document_id", "signal_type", name="uq_cost_signal_doc_type"
        ),
    )
    # Column-level indexes (column has index=True in model)
    op.create_index("ix_cost_signals_id", "cost_signals", ["id"])
    op.create_index("ix_cost_signals_case_id", "cost_signals", ["case_id"])
    op.create_index("ix_cost_signals_proceeding_id", "cost_signals", ["proceeding_id"])
    op.create_index("ix_cost_signals_signal_type", "cost_signals", ["signal_type"])
    op.create_index("ix_cost_signals_issued_at", "cost_signals", ["issued_at"])
    # Composite index for "current signal of this type for proceeding" lookups
    op.create_index(
        "ix_cost_signals_proc_type",
        "cost_signals",
        ["proceeding_id", "signal_type"],
    )

    # Backfill from Document.cost_delta — only the four orphan kinds.
    # Invoice/vorschuss kinds already live in legal_costs (via prior
    # ensure_ledger_row_for_signal calls); we leave them alone.
    conn = op.get_bind()
    rows = conn.execute(
        sa.text(
            "SELECT id, case_id, proceeding_id, cost_delta, issued_date, ingest_date "
            "FROM documents WHERE cost_delta IS NOT NULL"
        )
    ).fetchall()
    for doc_id, case_id, proc_id, cost_delta_json, issued_date, ingest_date in rows:
        if not cost_delta_json or not case_id:
            continue
        cd = (
            json.loads(cost_delta_json)
            if isinstance(cost_delta_json, str)
            else cost_delta_json
        )
        if not isinstance(cd, dict):
            continue
        kind = cd.get("kind")
        if kind not in _BACKFILL_KINDS:
            continue
        allocation = cd.get("allocation")
        conn.execute(
            sa.text(
                "INSERT INTO cost_signals "
                "(case_id, proceeding_id, source_document_id, signal_type, "
                "amount, allocation, description, issued_at, ingest_date) "
                "VALUES (:case_id, :proc_id, :doc_id, :sig_type, "
                ":amount, :allocation, :description, :issued_at, :ingest_date)"
            ),
            {
                "case_id": case_id,
                "proc_id": proc_id,
                "doc_id": doc_id,
                "sig_type": kind,
                "amount": cd.get("amount"),
                "allocation": json.dumps(allocation) if allocation else None,
                "description": cd.get("description"),
                "issued_at": issued_date,
                "ingest_date": ingest_date,
            },
        )


def downgrade() -> None:
    op.drop_index("ix_cost_signals_proc_type", table_name="cost_signals")
    op.drop_index("ix_cost_signals_issued_at", table_name="cost_signals")
    op.drop_index("ix_cost_signals_signal_type", table_name="cost_signals")
    op.drop_index("ix_cost_signals_proceeding_id", table_name="cost_signals")
    op.drop_index("ix_cost_signals_case_id", table_name="cost_signals")
    op.drop_index("ix_cost_signals_id", table_name="cost_signals")
    op.drop_table("cost_signals")
