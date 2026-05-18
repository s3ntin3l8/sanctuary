"""add_document_pipeline_stages_table

Revision ID: 05a267c5420a
Revises: a4bef00d
Create Date: 2026-05-17 23:51:42.360568

"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

# revision identifiers, used by Alembic.
revision: str = "05a267c5420a"
down_revision: str | Sequence[str] | None = "a4bef00d"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def _coerce_dt(val: str | None) -> str | None:
    """Normalise any ISO 8601 timestamp to SQLAlchemy-SQLite's space-sep format.

    pipeline_status.py writes UTC ISO strings like "2026-05-17T12:34:56.789012+00:00".
    SQLAlchemy's SQLite DateTime reads/writes "YYYY-MM-DD HH:MM:SS.ffffff" (no T, no tz).
    """
    if not val:
        return None
    import re

    val = re.sub(r"T", " ", val, count=1)
    val = re.sub(r"[+-]\d{2}:\d{2}$", "", val)
    return val


def upgrade() -> None:
    import json

    op.create_table(
        "document_pipeline_stages",
        sa.Column(
            "document_id",
            sa.Integer(),
            sa.ForeignKey("documents.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column("stage", sa.String(), nullable=False),
        sa.Column("status", sa.String(), nullable=False),
        sa.Column("started_at", sa.DateTime(), nullable=True),
        sa.Column("completed_at", sa.DateTime(), nullable=True),
        sa.Column("error", sa.Text(), nullable=True),
        sa.Column("reason", sa.String(), nullable=True),
        sa.Column("attempt", sa.Integer(), nullable=True),
        sa.Column("max_attempts", sa.Integer(), nullable=True),
        sa.Column("next_at", sa.DateTime(), nullable=True),
        sa.PrimaryKeyConstraint("document_id", "stage"),
    )
    op.create_index("ix_dps_status", "document_pipeline_stages", ["status"])
    op.create_index(
        "ix_dps_stage_status", "document_pipeline_stages", ["stage", "status"]
    )

    # Backfill from JSON
    conn = op.get_bind()
    rows = conn.execute(
        sa.text(
            "SELECT id, pipeline_stages FROM documents WHERE pipeline_stages IS NOT NULL"
        )
    ).fetchall()
    for doc_id, stages_json in rows:
        if not stages_json:
            continue
        stages = (
            json.loads(stages_json) if isinstance(stages_json, str) else stages_json
        )
        if not isinstance(stages, dict):
            continue
        for stage_key, stage_data in stages.items():
            if not isinstance(stage_data, dict) or "status" not in stage_data:
                continue
            conn.execute(
                sa.text(
                    "INSERT INTO document_pipeline_stages "
                    "(document_id, stage, status, started_at, completed_at, error, reason, attempt, max_attempts, next_at) "
                    "VALUES (:doc, :stage, :status, :started, :completed, :error, :reason, :attempt, :max_attempts, :next_at)"
                ),
                {
                    "doc": doc_id,
                    "stage": stage_key,
                    "status": stage_data.get("status"),
                    "started": _coerce_dt(stage_data.get("started_at")),
                    "completed": _coerce_dt(stage_data.get("completed_at")),
                    "error": stage_data.get("error"),
                    "reason": stage_data.get("reason"),
                    "attempt": stage_data.get("attempt"),
                    "max_attempts": stage_data.get("max_attempts"),
                    "next_at": _coerce_dt(stage_data.get("next_at")),
                },
            )


def downgrade() -> None:
    op.drop_index("ix_dps_stage_status", table_name="document_pipeline_stages")
    op.drop_index("ix_dps_status", table_name="document_pipeline_stages")
    op.drop_table("document_pipeline_stages")
