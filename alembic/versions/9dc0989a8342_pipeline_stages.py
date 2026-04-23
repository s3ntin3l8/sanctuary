"""Add pipeline_state and pipeline_stages; drop legacy ingest/ai_summary_status columns

Revision ID: 9dc0989a8342
Revises: 8ef2d25dee29
Create Date: 2026-04-23 00:00:00.000000

"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "9dc0989a8342"
down_revision: str | Sequence[str] | None = "8ef2d25dee29"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    # 1. Add new columns
    op.add_column(
        "documents",
        sa.Column(
            "pipeline_state",
            sa.String(16),
            nullable=False,
            server_default="pending",
        ),
    )
    op.add_column(
        "documents",
        sa.Column("pipeline_stages", sa.JSON(), nullable=True),
    )
    op.create_index("ix_documents_pipeline_state", "documents", ["pipeline_state"])

    # 2. Backfill pipeline_stages + pipeline_state from legacy columns
    #    (best-effort; pre-release test data only)
    op.execute(
        sa.text(
            """
            UPDATE documents
            SET pipeline_stages = json_object(
                'extract',
                CASE
                    WHEN ingest_status IN ('completed', 'COMPLETED')
                         THEN json_object('status','completed')
                    WHEN ingest_status IN ('failed', 'FAILED')
                         THEN json_object('status','failed','error', COALESCE(ingest_error,''))
                    ELSE json_object('status','pending')
                END,
                'metadata',
                CASE
                    WHEN ai_summary_status IN ('generated','approved')
                         THEN json_object('status','completed')
                    ELSE json_object('status','pending')
                END,
                'batch_analysis',
                json_object('status','skipped','reason','pre-migration'),
                'enrich',
                CASE
                    WHEN ai_summary IS NOT NULL
                         THEN json_object('status','completed')
                    WHEN ai_summary_status = 'failed'
                         THEN json_object('status','failed')
                    ELSE json_object('status','pending')
                END,
                'relationships',
                CASE
                    WHEN EXISTS (
                        SELECT 1 FROM document_relationships
                        WHERE from_document_id = documents.id
                    ) THEN json_object('status','completed')
                    ELSE json_object('status','pending')
                END,
                'claims',
                CASE
                    WHEN EXISTS (
                        SELECT 1 FROM claims
                        WHERE source_document_id = documents.id
                    ) THEN json_object('status','completed')
                    ELSE json_object('status','pending')
                END,
                'embeddings',
                CASE
                    WHEN EXISTS (
                        SELECT 1 FROM document_vectors
                        WHERE document_id = documents.id
                    ) THEN json_object('status','completed')
                    ELSE json_object('status','pending')
                END
            ),
            pipeline_state =
                CASE
                    WHEN ingest_status IN ('failed','FAILED') THEN 'failed'
                    WHEN ingest_status IN ('processing','PROCESSING') THEN 'running'
                    WHEN ingest_status IN ('completed','COMPLETED')
                         AND ai_summary IS NOT NULL THEN 'completed'
                    WHEN ingest_status IN ('completed','COMPLETED') THEN 'partial'
                    ELSE 'pending'
                END
            """
        )
    )

    # 3. Drop legacy columns
    with op.batch_alter_table("documents") as batch_op:
        batch_op.drop_column("ingest_status")
        batch_op.drop_column("ingest_error")
        batch_op.drop_column("ingest_started_at")
        batch_op.drop_column("ingest_completed_at")
        batch_op.drop_column("ai_summary_status")


def downgrade() -> None:
    # Restore legacy columns (data is not backfilled on downgrade)
    with op.batch_alter_table("documents") as batch_op:
        batch_op.add_column(sa.Column("ingest_status", sa.String(16), nullable=True))
        batch_op.add_column(sa.Column("ingest_error", sa.Text(), nullable=True))
        batch_op.add_column(
            sa.Column("ingest_started_at", sa.DateTime(), nullable=True)
        )
        batch_op.add_column(
            sa.Column("ingest_completed_at", sa.DateTime(), nullable=True)
        )
        batch_op.add_column(
            sa.Column("ai_summary_status", sa.String(32), nullable=True)
        )

    op.drop_index("ix_documents_pipeline_state", table_name="documents")
    with op.batch_alter_table("documents") as batch_op:
        batch_op.drop_column("pipeline_state")
        batch_op.drop_column("pipeline_stages")
