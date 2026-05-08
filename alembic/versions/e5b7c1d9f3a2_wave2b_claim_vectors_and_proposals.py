"""wave2b_claim_vectors_and_proposals

Revision ID: e5b7c1d9f3a2
Revises: d2c4f9a1b6e8
Create Date: 2026-05-08 00:00:00.000000

Wave 2B foundation: the schema needed for semantic dedup + AI-proposes /
user-confirms evidence-linking.

1. `claim_vectors` (vec0 virtual table): mirrors `document_vectors`. Embeds
   each claim's text so we can find top-K nearest claims for the dedup
   judge and the pre-extraction context. Dimension matches the live
   `document_vectors` dim (read at migration time to avoid drift).

2. `claim_merge_proposals`: AI-proposed merges from the dedup judge.
   "This new claim looks like an existing claim — merge?" User confirms
   or dismisses. Confirmed merges absorb the new claim's evidence into
   the existing claim and delete the new claim.

3. `claim_evidence_proposals`: AI-proposed cross-doc stances on existing
   claims. Replaces the old auto-apply path in claim_extractor where
   evidence_links wrote ClaimEvidence rows directly (and sometimes
   wrongly auto-flipped status to REFUTED). Now the AI proposes; the
   user confirms before any evidence row or status change lands.
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "e5b7c1d9f3a2"
down_revision: str | Sequence[str] | None = "d2c4f9a1b6e8"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    # 1. claim_vectors — match document_vectors' declared dim.
    bind = op.get_bind()
    row = bind.execute(
        sa.text("SELECT sql FROM sqlite_master WHERE name = 'document_vectors'")
    ).fetchone()
    embed_dim = 768  # nomic-embed-text default fallback
    if row and row[0]:
        import re

        m = re.search(r"embedding\s+float\s*\[\s*(\d+)\s*\]", row[0], re.IGNORECASE)
        if m:
            embed_dim = int(m.group(1))

    op.execute(
        f"CREATE VIRTUAL TABLE IF NOT EXISTS claim_vectors "
        f"USING vec0(claim_id INTEGER PRIMARY KEY, embedding float[{embed_dim}])"
    )

    # 2. claim_merge_proposals
    op.create_table(
        "claim_merge_proposals",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column(
            "new_claim_id",
            sa.Integer(),
            sa.ForeignKey("claims.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column(
            "existing_claim_id",
            sa.Integer(),
            sa.ForeignKey("claims.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column("confidence", sa.String(length=10), nullable=False),
        sa.Column("rationale", sa.Text(), nullable=True),
        sa.Column(
            "status", sa.String(length=12), nullable=False, server_default="PENDING"
        ),
        sa.Column(
            "proposed_at",
            sa.DateTime(),
            nullable=False,
            server_default=sa.func.current_timestamp(),
        ),
        sa.Column("resolved_at", sa.DateTime(), nullable=True),
    )
    op.create_index(
        "ix_claim_merge_proposals_status_pending",
        "claim_merge_proposals",
        ["status"],
        sqlite_where=sa.text("status = 'PENDING'"),
    )
    op.create_index(
        "ix_claim_merge_proposals_new_claim",
        "claim_merge_proposals",
        ["new_claim_id"],
    )
    op.create_index(
        "ix_claim_merge_proposals_existing_claim",
        "claim_merge_proposals",
        ["existing_claim_id"],
    )

    # 3. claim_evidence_proposals
    op.create_table(
        "claim_evidence_proposals",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column(
            "target_claim_id",
            sa.Integer(),
            sa.ForeignKey("claims.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column(
            "source_document_id",
            sa.Integer(),
            sa.ForeignKey("documents.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column("proposed_role", sa.String(length=20), nullable=False),
        sa.Column("excerpt", sa.Text(), nullable=True),
        sa.Column("rationale", sa.Text(), nullable=True),
        sa.Column("confidence", sa.String(length=10), nullable=False),
        sa.Column(
            "status", sa.String(length=12), nullable=False, server_default="PENDING"
        ),
        sa.Column(
            "proposed_at",
            sa.DateTime(),
            nullable=False,
            server_default=sa.func.current_timestamp(),
        ),
        sa.Column("resolved_at", sa.DateTime(), nullable=True),
    )
    op.create_index(
        "ix_claim_evidence_proposals_status_pending",
        "claim_evidence_proposals",
        ["status"],
        sqlite_where=sa.text("status = 'PENDING'"),
    )
    op.create_index(
        "ix_claim_evidence_proposals_target_claim",
        "claim_evidence_proposals",
        ["target_claim_id"],
    )
    op.create_index(
        "ix_claim_evidence_proposals_source_document",
        "claim_evidence_proposals",
        ["source_document_id"],
    )


def downgrade() -> None:
    op.drop_index(
        "ix_claim_evidence_proposals_source_document",
        table_name="claim_evidence_proposals",
    )
    op.drop_index(
        "ix_claim_evidence_proposals_target_claim",
        table_name="claim_evidence_proposals",
    )
    op.drop_index(
        "ix_claim_evidence_proposals_status_pending",
        table_name="claim_evidence_proposals",
    )
    op.drop_table("claim_evidence_proposals")
    op.drop_index(
        "ix_claim_merge_proposals_existing_claim",
        table_name="claim_merge_proposals",
    )
    op.drop_index(
        "ix_claim_merge_proposals_new_claim",
        table_name="claim_merge_proposals",
    )
    op.drop_index(
        "ix_claim_merge_proposals_status_pending",
        table_name="claim_merge_proposals",
    )
    op.drop_table("claim_merge_proposals")
    op.execute("DROP TABLE IF EXISTS claim_vectors")
