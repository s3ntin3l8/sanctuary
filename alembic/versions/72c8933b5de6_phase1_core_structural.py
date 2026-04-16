"""phase1: core structural (Proceeding, IngestBatch, DocumentRelationship, ActionItem)

Revision ID: 72c8933b5de6
Revises: 711ce72418b0
Create Date: 2026-04-16 00:00:00.000000

Phase 1 — Migration A. Adds the case-intelligence foundation:

* New tables:
    - proceedings (court-level stages inside a case)
    - ingest_batches (one email = one batch; documents arrive as families)
    - document_relationships (typed N:N edges)
    - action_items (consolidates deadlines + hearings + response/filing requirements)
* New columns on documents:
    - ingest_batch_id, proceeding_id, role, court_relay, attributed_originator,
      document_type, significance_tier, thread_open
* New columns on cases:
    - ai_brief, ai_brief_updated_at, parties, total_cost_exposure
* Migrates existing Deadline + Hearing rows into action_items (action_type set
  accordingly) and drops the old tables.
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "72c8933b5de6"
down_revision: str | Sequence[str] | None = "711ce72418b0"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    # --- 1. New tables -------------------------------------------------------
    op.create_table(
        "proceedings",
        sa.Column("id", sa.Integer(), nullable=False),
        sa.Column("case_id", sa.String(), nullable=False),
        sa.Column("court_name", sa.String(), nullable=False),
        sa.Column("court_level", sa.String(), nullable=False),
        sa.Column("subject_matter", sa.String(), nullable=True),
        sa.Column("az_court", sa.String(), nullable=True),
        sa.Column("status", sa.String(), nullable=False, server_default="active"),
        sa.Column("started_at", sa.DateTime(), nullable=True),
        sa.Column("ended_at", sa.DateTime(), nullable=True),
        sa.Column("created_at", sa.DateTime(), nullable=False),
        sa.ForeignKeyConstraint(["case_id"], ["cases.id"]),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index("ix_proceedings_id", "proceedings", ["id"])
    op.create_index("ix_proceedings_case", "proceedings", ["case_id"])
    op.create_index("ix_proceedings_case_status", "proceedings", ["case_id", "status"])

    op.create_table(
        "ingest_batches",
        sa.Column("id", sa.Integer(), nullable=False),
        sa.Column("source_type", sa.String(), nullable=False),
        sa.Column("received_at", sa.DateTime(), nullable=False),
        sa.Column("sender_email", sa.String(), nullable=True),
        sa.Column("subject", sa.String(), nullable=True),
        sa.Column("raw_source_path", sa.String(), nullable=True),
        sa.Column("case_id", sa.String(), nullable=True),
        sa.Column("proceeding_id", sa.Integer(), nullable=True),
        sa.Column("status", sa.String(), nullable=False, server_default="pending"),
        sa.Column("created_at", sa.DateTime(), nullable=False),
        sa.ForeignKeyConstraint(["case_id"], ["cases.id"]),
        sa.ForeignKeyConstraint(["proceeding_id"], ["proceedings.id"]),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index("ix_ingest_batches_id", "ingest_batches", ["id"])
    op.create_index("ix_ingest_batches_case", "ingest_batches", ["case_id"])
    op.create_index("ix_ingest_batches_received", "ingest_batches", ["received_at"])

    op.create_table(
        "document_relationships",
        sa.Column("id", sa.Integer(), nullable=False),
        sa.Column("from_document_id", sa.Integer(), nullable=False),
        sa.Column("to_document_id", sa.Integer(), nullable=False),
        sa.Column("relationship_type", sa.String(), nullable=False),
        sa.Column(
            "confidence",
            sa.String(),
            nullable=False,
            server_default="ai_detected",
        ),
        sa.Column("notes", sa.Text(), nullable=True),
        sa.Column("created_at", sa.DateTime(), nullable=False),
        sa.ForeignKeyConstraint(["from_document_id"], ["documents.id"]),
        sa.ForeignKeyConstraint(["to_document_id"], ["documents.id"]),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index("ix_document_relationships_id", "document_relationships", ["id"])
    op.create_index(
        "ix_document_relationships_from",
        "document_relationships",
        ["from_document_id"],
    )
    op.create_index(
        "ix_document_relationships_to",
        "document_relationships",
        ["to_document_id"],
    )
    op.create_index(
        "ix_document_relationships_type",
        "document_relationships",
        ["relationship_type"],
    )

    op.create_table(
        "action_items",
        sa.Column("id", sa.Integer(), nullable=False),
        sa.Column("case_id", sa.String(), nullable=False),
        sa.Column("proceeding_id", sa.Integer(), nullable=True),
        sa.Column("source_document_id", sa.Integer(), nullable=True),
        sa.Column("title", sa.String(), nullable=False),
        sa.Column("description", sa.Text(), nullable=True),
        sa.Column("due_date", sa.DateTime(), nullable=False),
        sa.Column(
            "action_type",
            sa.String(),
            nullable=False,
            server_default="deadline",
        ),
        sa.Column("status", sa.String(), nullable=False, server_default="open"),
        sa.Column("location", sa.String(), nullable=True),
        sa.Column("created_at", sa.DateTime(), nullable=False),
        sa.ForeignKeyConstraint(["case_id"], ["cases.id"]),
        sa.ForeignKeyConstraint(["proceeding_id"], ["proceedings.id"]),
        sa.ForeignKeyConstraint(["source_document_id"], ["documents.id"]),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index("ix_action_items_id", "action_items", ["id"])
    op.create_index("ix_action_items_case_id", "action_items", ["case_id"])
    op.create_index("ix_action_items_due_date", "action_items", ["due_date"])
    op.create_index("ix_action_items_case_due", "action_items", ["case_id", "due_date"])
    op.create_index(
        "ix_action_items_due_status", "action_items", ["due_date", "status"]
    )
    op.create_index("ix_action_items_proceeding", "action_items", ["proceeding_id"])
    op.create_index(
        "ix_action_items_source_document",
        "action_items",
        ["source_document_id"],
    )

    # --- 2. Extend documents -----------------------------------------------
    op.add_column(
        "documents", sa.Column("ingest_batch_id", sa.Integer(), nullable=True)
    )
    op.add_column("documents", sa.Column("proceeding_id", sa.Integer(), nullable=True))
    op.add_column(
        "documents",
        sa.Column("role", sa.String(), nullable=False, server_default="standalone"),
    )
    op.add_column(
        "documents",
        sa.Column("court_relay", sa.Boolean(), nullable=False, server_default="0"),
    )
    op.add_column(
        "documents",
        sa.Column("attributed_originator", sa.String(), nullable=True),
    )
    op.add_column("documents", sa.Column("document_type", sa.String(), nullable=True))
    op.add_column(
        "documents",
        sa.Column("significance_tier", sa.String(), nullable=True),
    )
    op.add_column(
        "documents",
        sa.Column("thread_open", sa.Boolean(), nullable=False, server_default="0"),
    )
    op.create_index("ix_documents_proceeding", "documents", ["proceeding_id"])
    op.create_index("ix_documents_ingest_batch", "documents", ["ingest_batch_id"])
    op.create_index("ix_documents_significance", "documents", ["significance_tier"])

    # --- 3. Extend cases ----------------------------------------------------
    op.add_column("cases", sa.Column("ai_brief", sa.JSON(), nullable=True))
    op.add_column(
        "cases", sa.Column("ai_brief_updated_at", sa.DateTime(), nullable=True)
    )
    op.add_column("cases", sa.Column("parties", sa.JSON(), nullable=True))
    op.add_column(
        "cases",
        sa.Column(
            "total_cost_exposure",
            sa.Integer(),
            nullable=False,
            server_default="0",
        ),
    )

    # --- 4. Migrate Deadline + Hearing → action_items -----------------------
    conn = op.get_bind()

    # Move deadlines first (action_type='deadline', status derived from completed)
    conn.execute(
        sa.text(
            """
            INSERT INTO action_items (
                case_id, source_document_id, title, description,
                due_date, action_type, status, location, created_at
            )
            SELECT
                case_id,
                source_document_id,
                title,
                description,
                due_at,
                'deadline',
                CASE WHEN completed = 1 THEN 'completed' ELSE 'open' END,
                NULL,
                created_at
            FROM deadlines
            """
        )
    )

    # Move hearings next (action_type='court_date', all status='open')
    conn.execute(
        sa.text(
            """
            INSERT INTO action_items (
                case_id, source_document_id, title, description,
                due_date, action_type, status, location, created_at
            )
            SELECT
                case_id,
                source_document_id,
                title,
                description,
                scheduled_for,
                'court_date',
                'open',
                location,
                created_at
            FROM hearings
            """
        )
    )

    # --- 5. Drop old tables -------------------------------------------------
    for idx in (
        "ix_deadlines_case_due",
        "ix_deadlines_due_completed",
        "ix_deadlines_due_at",
        "ix_deadlines_case_id",
        "ix_deadlines_source_document_id",
        "ix_deadlines_id",
    ):
        try:
            op.drop_index(idx, table_name="deadlines")
        except Exception:
            pass
    op.drop_table("deadlines")

    # index names vary across historical migrations; drop what exists
    for idx in (
        "ix_hearings_case_scheduled",
        "ix_hearings_scheduled",
        "ix_hearings_scheduled_for",
        "ix_hearings_source_document_id",
        "ix_hearings_case_id",
        "ix_hearings_id",
    ):
        try:
            op.drop_index(idx, table_name="hearings")
        except Exception:
            pass
    op.drop_table("hearings")


def downgrade() -> None:
    # --- 1. Recreate deadlines / hearings -----------------------------------
    op.create_table(
        "deadlines",
        sa.Column("id", sa.Integer(), nullable=False),
        sa.Column("case_id", sa.String(), nullable=False),
        sa.Column("title", sa.String(), nullable=False),
        sa.Column("description", sa.Text(), nullable=True),
        sa.Column("due_at", sa.DateTime(), nullable=False),
        sa.Column(
            "completed",
            sa.Boolean(),
            nullable=False,
            server_default="0",
        ),
        sa.Column("source_document_id", sa.Integer(), nullable=True),
        sa.Column("created_at", sa.DateTime(), nullable=False),
        sa.ForeignKeyConstraint(["case_id"], ["cases.id"]),
        sa.ForeignKeyConstraint(["source_document_id"], ["documents.id"]),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index("ix_deadlines_case_due", "deadlines", ["case_id", "due_at"])
    op.create_index("ix_deadlines_due_completed", "deadlines", ["due_at", "completed"])

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
    op.create_index(
        "ix_hearings_case_scheduled", "hearings", ["case_id", "scheduled_for"]
    )
    op.create_index("ix_hearings_scheduled", "hearings", ["scheduled_for"])

    conn = op.get_bind()

    # Restore deadlines from action_items
    conn.execute(
        sa.text(
            """
            INSERT INTO deadlines (
                case_id, source_document_id, title, description,
                due_at, completed, created_at
            )
            SELECT
                case_id,
                source_document_id,
                title,
                description,
                due_date,
                CASE WHEN status = 'completed' THEN 1 ELSE 0 END,
                created_at
            FROM action_items
            WHERE action_type = 'deadline'
            """
        )
    )

    # Restore hearings from action_items
    conn.execute(
        sa.text(
            """
            INSERT INTO hearings (
                case_id, source_document_id, title, description,
                scheduled_for, location, created_at
            )
            SELECT
                case_id,
                source_document_id,
                title,
                description,
                due_date,
                location,
                created_at
            FROM action_items
            WHERE action_type = 'court_date'
            """
        )
    )

    # --- 2. Drop new columns on cases --------------------------------------
    op.drop_column("cases", "total_cost_exposure")
    op.drop_column("cases", "parties")
    op.drop_column("cases", "ai_brief_updated_at")
    op.drop_column("cases", "ai_brief")

    # --- 3. Drop new columns on documents ----------------------------------
    op.drop_index("ix_documents_significance", table_name="documents")
    op.drop_index("ix_documents_ingest_batch", table_name="documents")
    op.drop_index("ix_documents_proceeding", table_name="documents")
    op.drop_column("documents", "thread_open")
    op.drop_column("documents", "significance_tier")
    op.drop_column("documents", "document_type")
    op.drop_column("documents", "attributed_originator")
    op.drop_column("documents", "court_relay")
    op.drop_column("documents", "role")
    op.drop_column("documents", "proceeding_id")
    op.drop_column("documents", "ingest_batch_id")

    # --- 4. Drop new tables -------------------------------------------------
    op.drop_index("ix_action_items_source_document", table_name="action_items")
    op.drop_index("ix_action_items_proceeding", table_name="action_items")
    op.drop_index("ix_action_items_due_status", table_name="action_items")
    op.drop_index("ix_action_items_case_due", table_name="action_items")
    op.drop_index("ix_action_items_due_date", table_name="action_items")
    op.drop_index("ix_action_items_case_id", table_name="action_items")
    op.drop_index("ix_action_items_id", table_name="action_items")
    op.drop_table("action_items")

    op.drop_index("ix_document_relationships_type", table_name="document_relationships")
    op.drop_index("ix_document_relationships_to", table_name="document_relationships")
    op.drop_index("ix_document_relationships_from", table_name="document_relationships")
    op.drop_index("ix_document_relationships_id", table_name="document_relationships")
    op.drop_table("document_relationships")

    op.drop_index("ix_ingest_batches_received", table_name="ingest_batches")
    op.drop_index("ix_ingest_batches_case", table_name="ingest_batches")
    op.drop_index("ix_ingest_batches_id", table_name="ingest_batches")
    op.drop_table("ingest_batches")

    op.drop_index("ix_proceedings_case_status", table_name="proceedings")
    op.drop_index("ix_proceedings_case", table_name="proceedings")
    op.drop_index("ix_proceedings_id", table_name="proceedings")
    op.drop_table("proceedings")
