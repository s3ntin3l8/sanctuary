"""initial full schema

Revision ID: 698c5f71bf23
Revises:
Create Date: 2026-04-05 02:14:47.337112

"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "698c5f71bf23"
down_revision: str | Sequence[str] | None = None
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    """Upgrade schema — idempotent for both fresh and existing databases."""
    conn = op.get_bind()
    existing_tables = conn.execute(
        sa.text("SELECT name FROM sqlite_master WHERE type='table'")
    ).fetchall()
    existing_tables = {row[0] for row in existing_tables}

    if "cases" not in existing_tables:
        op.create_table(
            "cases",
            sa.Column("id", sa.String(), nullable=False),
            sa.Column("title", sa.String(), nullable=False),
            sa.Column("court_id", sa.String(), nullable=True),
            sa.Column("status", sa.String(), nullable=False),
            sa.Column("created_at", sa.DateTime(), nullable=True),
            sa.Column("closed_at", sa.DateTime(), nullable=True),
            sa.PrimaryKeyConstraint("id"),
        )
        op.create_index(op.f("ix_cases_id"), "cases", ["id"], unique=False)

    if "documents" not in existing_tables:
        op.create_table(
            "documents",
            sa.Column("id", sa.Integer(), nullable=False),
            sa.Column("title", sa.String(), nullable=False),
            sa.Column("content", sa.Text(), nullable=True),
            sa.Column("content_embedding", sa.Text(), nullable=True),
            sa.Column("case_id", sa.String(), nullable=True),
            sa.Column("file_path", sa.String(), nullable=True),
            sa.Column("originator_type", sa.String(), nullable=False),
            sa.Column("sender", sa.String(), nullable=True),
            sa.Column("received_date", sa.DateTime(), nullable=True),
            sa.Column("created_at", sa.DateTime(), nullable=True),
            sa.Column("needs_review", sa.Boolean(), nullable=True),
            sa.Column("review_reasons", sa.JSON(), nullable=True),
            sa.Column("ai_summary", sa.JSON(), nullable=True),
            sa.Column("ai_summary_created_at", sa.DateTime(), nullable=True),
            sa.Column(
                "ai_summary_status",
                sa.String(),
                nullable=False,
                server_default="pending",
            ),
            sa.Column("parent_id", sa.Integer(), nullable=True),
            sa.ForeignKeyConstraint(["parent_id"], ["documents.id"]),
            sa.PrimaryKeyConstraint("id"),
        )
        op.create_index(op.f("ix_documents_id"), "documents", ["id"], unique=False)
        op.create_index(
            op.f("ix_documents_title"), "documents", ["title"], unique=False
        )
        op.create_index(
            op.f("ix_documents_case_id"), "documents", ["case_id"], unique=False
        )
        op.create_index(
            op.f("ix_documents_needs_review"),
            "documents",
            ["needs_review"],
            unique=False,
        )

    if "deadlines" not in existing_tables:
        op.create_table(
            "deadlines",
            sa.Column("id", sa.Integer(), nullable=False),
            sa.Column("case_id", sa.String(), nullable=False),
            sa.Column("title", sa.String(), nullable=False),
            sa.Column("description", sa.Text(), nullable=True),
            sa.Column("due_at", sa.DateTime(), nullable=False),
            sa.Column("completed", sa.Boolean(), nullable=False),
            sa.Column("source_document_id", sa.Integer(), nullable=True),
            sa.Column("created_at", sa.DateTime(), nullable=False),
            sa.ForeignKeyConstraint(["case_id"], ["cases.id"]),
            sa.ForeignKeyConstraint(["source_document_id"], ["documents.id"]),
            sa.PrimaryKeyConstraint("id"),
        )
        op.create_index(op.f("ix_deadlines_id"), "deadlines", ["id"], unique=False)
        op.create_index(
            op.f("ix_deadlines_case_id"), "deadlines", ["case_id"], unique=False
        )
        op.create_index(
            op.f("ix_deadlines_due_at"), "deadlines", ["due_at"], unique=False
        )
        op.create_index(
            op.f("ix_deadlines_completed"), "deadlines", ["completed"], unique=False
        )
        op.create_index(
            op.f("ix_deadlines_source_document_id"),
            "deadlines",
            ["source_document_id"],
            unique=False,
        )

    if "hearings" not in existing_tables:
        op.create_table(
            "hearings",
            sa.Column("id", sa.Integer(), nullable=False),
            sa.Column("case_id", sa.String(), nullable=False),
            sa.Column("title", sa.String(), nullable=False),
            sa.Column("description", sa.Text(), nullable=True),
            sa.Column("scheduled_for", sa.DateTime(), nullable=False),
            sa.Column("location", sa.String(), nullable=True),
            sa.Column("source_document_id", sa.Integer(), nullable=True),
            sa.Column("created_at", sa.DateTime(), nullable=False),
            sa.ForeignKeyConstraint(["case_id"], ["cases.id"]),
            sa.ForeignKeyConstraint(["source_document_id"], ["documents.id"]),
            sa.PrimaryKeyConstraint("id"),
        )
        op.create_index(op.f("ix_hearings_id"), "hearings", ["id"], unique=False)
        op.create_index(
            op.f("ix_hearings_case_id"), "hearings", ["case_id"], unique=False
        )
        op.create_index(
            op.f("ix_hearings_scheduled_for"),
            "hearings",
            ["scheduled_for"],
            unique=False,
        )
        op.create_index(
            op.f("ix_hearings_source_document_id"),
            "hearings",
            ["source_document_id"],
            unique=False,
        )

    if "legal_costs" not in existing_tables:
        op.create_table(
            "legal_costs",
            sa.Column("id", sa.Integer(), nullable=False),
            sa.Column("case_id", sa.String(), nullable=False),
            sa.Column("category", sa.String(), nullable=False),
            sa.Column("status", sa.String(), nullable=False),
            sa.Column("title", sa.String(), nullable=False),
            sa.Column("rvg_position", sa.String(), nullable=True),
            sa.Column("amount_net", sa.Float(), nullable=False),
            sa.Column("vat_rate", sa.Float(), nullable=True),
            sa.Column("amount_gross", sa.Float(), nullable=False),
            sa.Column("amount_paid", sa.Float(), nullable=True),
            sa.Column("amount_reimbursed", sa.Float(), nullable=True),
            sa.Column("streitwert", sa.Float(), nullable=True),
            sa.Column("gebuehren_faktor", sa.Float(), nullable=True),
            sa.Column("is_reimbursable", sa.Boolean(), nullable=True),
            sa.Column("issued_at", sa.DateTime(), nullable=True),
            sa.Column("due_at", sa.DateTime(), nullable=True),
            sa.Column("paid_at", sa.DateTime(), nullable=True),
            sa.Column("source_document_id", sa.Integer(), nullable=True),
            sa.Column("notes", sa.Text(), nullable=True),
            sa.Column("created_at", sa.DateTime(), nullable=False),
            sa.ForeignKeyConstraint(["case_id"], ["cases.id"]),
            sa.ForeignKeyConstraint(["source_document_id"], ["documents.id"]),
            sa.PrimaryKeyConstraint("id"),
        )
        op.create_index(op.f("ix_legal_costs_id"), "legal_costs", ["id"], unique=False)
        op.create_index(
            op.f("ix_legal_costs_case_id"), "legal_costs", ["case_id"], unique=False
        )
        op.create_index(
            op.f("ix_legal_costs_status"), "legal_costs", ["status"], unique=False
        )

    # Add missing columns to existing tables
    if "documents" in existing_tables:
        doc_cols = conn.execute(sa.text("PRAGMA table_info(documents)")).fetchall()
        doc_col_names = {row[1] for row in doc_cols}
        if "content_embedding" not in doc_col_names:
            op.add_column(
                "documents", sa.Column("content_embedding", sa.Text(), nullable=True)
            )
        if "ai_summary" not in doc_col_names:
            op.add_column(
                "documents", sa.Column("ai_summary", sa.JSON(), nullable=True)
            )
        if "ai_summary_created_at" not in doc_col_names:
            op.add_column(
                "documents",
                sa.Column("ai_summary_created_at", sa.DateTime(), nullable=True),
            )
        if "ai_summary_status" not in doc_col_names:
            op.add_column(
                "documents",
                sa.Column(
                    "ai_summary_status",
                    sa.String(),
                    nullable=False,
                    server_default="pending",
                ),
            )

    # Rename document_id → source_document_id in legal_costs
    if "legal_costs" in existing_tables:
        lc_cols = conn.execute(sa.text("PRAGMA table_info(legal_costs)")).fetchall()
        lc_col_names = {row[1] for row in lc_cols}
        if "document_id" in lc_col_names and "source_document_id" not in lc_col_names:
            with op.batch_alter_table("legal_costs") as batch_op:
                batch_op.alter_column(
                    "document_id", new_column_name="source_document_id"
                )


def downgrade() -> None:
    """Downgrade schema."""
    op.drop_index(op.f("ix_legal_costs_status"), table_name="legal_costs")
    op.drop_index(op.f("ix_legal_costs_case_id"), table_name="legal_costs")
    op.drop_index(op.f("ix_legal_costs_id"), table_name="legal_costs")
    op.drop_table("legal_costs")
    op.drop_index(op.f("ix_hearings_source_document_id"), table_name="hearings")
    op.drop_index(op.f("ix_hearings_scheduled_for"), table_name="hearings")
    op.drop_index(op.f("ix_hearings_case_id"), table_name="hearings")
    op.drop_index(op.f("ix_hearings_id"), table_name="hearings")
    op.drop_table("hearings")
    op.drop_index(op.f("ix_deadlines_source_document_id"), table_name="deadlines")
    op.drop_index(op.f("ix_deadlines_completed"), table_name="deadlines")
    op.drop_index(op.f("ix_deadlines_due_at"), table_name="deadlines")
    op.drop_index(op.f("ix_deadlines_case_id"), table_name="deadlines")
    op.drop_index(op.f("ix_deadlines_id"), table_name="deadlines")
    op.drop_table("deadlines")
    op.drop_index(op.f("ix_documents_needs_review"), table_name="documents")
    op.drop_index(op.f("ix_documents_case_id"), table_name="documents")
    op.drop_index(op.f("ix_documents_title"), table_name="documents")
    op.drop_index(op.f("ix_documents_id"), table_name="documents")
    op.drop_table("documents")
    op.drop_index(op.f("ix_cases_id"), table_name="cases")
    op.drop_table("cases")
