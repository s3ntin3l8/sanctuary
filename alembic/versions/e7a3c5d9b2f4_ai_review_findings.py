"""Second-opinion findings on AI output (ai_review_findings)

Revision ID: e7a3c5d9b2f4
Revises: d2e8b4f6a1c3
Create Date: 2026-11-27 16:00:00.000000

"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

# revision identifiers, used by Alembic.
revision: str = "e7a3c5d9b2f4"  # pragma: allowlist secret
down_revision: str | Sequence[str] | None = "d2e8b4f6a1c3"  # pragma: allowlist secret
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

_TARGET_KINDS = (
    "document_field",
    "summary",
    "case_assignment",
    "relationship",
    "claim",
    "claim_evidence_proposal",
    "claim_merge_proposal",
    "action_item",
    "cost_signal",
    "entity",
    "ocr_text",
    "case_brief",
)


def upgrade() -> None:
    op.create_table(
        "ai_review_findings",
        sa.Column("id", sa.Integer(), nullable=False),
        sa.Column(
            "reviewer_kind",
            sa.Enum("agent", "verifier", name="reviewerkind", native_enum=False),
            nullable=False,
        ),
        sa.Column("agent_token_id", sa.Integer(), nullable=True),
        sa.Column(
            "target_kind",
            sa.Enum(*_TARGET_KINDS, name="reviewtargetkind", native_enum=False),
            nullable=False,
        ),
        sa.Column("document_id", sa.Integer(), nullable=True),
        sa.Column("case_id", sa.String(), nullable=False),
        sa.Column("target_id", sa.Integer(), nullable=True),
        sa.Column("field", sa.String(), nullable=True),
        sa.Column(
            "verdict",
            sa.Enum(
                "agree",
                "disagree",
                "uncertain",
                name="reviewverdict",
                native_enum=False,
            ),
            nullable=False,
        ),
        sa.Column("ai_value", postgresql.JSONB(astext_type=sa.Text()), nullable=True),
        sa.Column(
            "proposed_value", postgresql.JSONB(astext_type=sa.Text()), nullable=True
        ),
        sa.Column("rationale", sa.Text(), nullable=False),
        sa.Column("cited_excerpt", sa.Text(), nullable=True),
        sa.Column("reviewed_model", sa.String(), nullable=True),
        sa.Column("reviewed_prompt_version", sa.String(), nullable=True),
        sa.Column(
            "status",
            sa.Enum(
                "pending",
                "confirmed",
                "dismissed",
                "stale",
                "noted",
                name="reviewfindingstatus",
                native_enum=False,
            ),
            nullable=False,
        ),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("resolved_at", sa.DateTime(timezone=True), nullable=True),
        sa.ForeignKeyConstraint(
            ["agent_token_id"], ["api_tokens.id"], ondelete="SET NULL"
        ),
        sa.ForeignKeyConstraint(["document_id"], ["documents.id"], ondelete="CASCADE"),
        sa.ForeignKeyConstraint(
            ["case_id"], ["cases.id"], ondelete="CASCADE", onupdate="CASCADE"
        ),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index(
        "ix_ai_review_findings_document_id", "ai_review_findings", ["document_id"]
    )
    op.create_index("ix_ai_review_findings_case_id", "ai_review_findings", ["case_id"])
    op.create_index(
        "ix_ai_review_findings_pending",
        "ai_review_findings",
        ["status"],
        postgresql_where=sa.text("status = 'pending'"),
    )
    op.create_index(
        "uq_ai_review_findings_pending_target",
        "ai_review_findings",
        [
            "reviewer_kind",
            sa.text("coalesce(agent_token_id, 0)"),
            "target_kind",
            "case_id",
            sa.text("coalesce(document_id, 0)"),
            sa.text("coalesce(target_id, 0)"),
            sa.text("coalesce(field, '')"),
        ],
        unique=True,
        postgresql_where=sa.text("status = 'pending'"),
    )


def downgrade() -> None:
    op.drop_index(
        "uq_ai_review_findings_pending_target", table_name="ai_review_findings"
    )
    op.drop_index("ix_ai_review_findings_pending", table_name="ai_review_findings")
    op.drop_index("ix_ai_review_findings_case_id", table_name="ai_review_findings")
    op.drop_index("ix_ai_review_findings_document_id", table_name="ai_review_findings")
    op.drop_table("ai_review_findings")
