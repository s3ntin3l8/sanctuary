"""phase1: Truth Map (Claim, ClaimEvidence, UserReaction, Document.key_passages+cost_delta)

Revision ID: 404c6c87d3f1
Revises: 72c8933b5de6
Create Date: 2026-04-16 00:00:01.000000

Phase 1 — Migration B. Purely additive.

* New tables:
    - claims (atomic factual/legal assertions per case)
    - claim_evidence (documents supporting/contesting/refuting a claim)
    - user_reactions (🚩/✅/🔍/⚖️ tagged by user during triage)
* New columns on documents:
    - key_passages (JSON) — AI-identified significant excerpts
    - cost_delta (JSON) — financial impact of this document
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "404c6c87d3f1"
down_revision: str | Sequence[str] | None = "72c8933b5de6"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    # --- claims --------------------------------------------------------------
    op.create_table(
        "claims",
        sa.Column("id", sa.Integer(), nullable=False),
        sa.Column("case_id", sa.String(), nullable=False),
        sa.Column("proceeding_id", sa.Integer(), nullable=True),
        sa.Column("source_document_id", sa.Integer(), nullable=False),
        sa.Column("claim_text", sa.Text(), nullable=False),
        sa.Column(
            "claim_type",
            sa.String(),
            nullable=False,
            server_default="factual",
        ),
        sa.Column("status", sa.String(), nullable=False, server_default="asserted"),
        sa.Column("first_made_at", sa.DateTime(), nullable=False),
        sa.Column("last_updated_at", sa.DateTime(), nullable=False),
        sa.ForeignKeyConstraint(["case_id"], ["cases.id"]),
        sa.ForeignKeyConstraint(["proceeding_id"], ["proceedings.id"]),
        sa.ForeignKeyConstraint(["source_document_id"], ["documents.id"]),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index("ix_claims_id", "claims", ["id"])
    op.create_index("ix_claims_case_id", "claims", ["case_id"])
    op.create_index("ix_claims_source_document", "claims", ["source_document_id"])
    op.create_index("ix_claims_case", "claims", ["case_id"])
    op.create_index("ix_claims_case_status", "claims", ["case_id", "status"])
    op.create_index("ix_claims_proceeding", "claims", ["proceeding_id"])

    # --- claim_evidence ------------------------------------------------------
    op.create_table(
        "claim_evidence",
        sa.Column("id", sa.Integer(), nullable=False),
        sa.Column("claim_id", sa.Integer(), nullable=False),
        sa.Column("document_id", sa.Integer(), nullable=False),
        sa.Column("role", sa.String(), nullable=False),
        sa.Column("excerpt", sa.Text(), nullable=True),
        sa.Column(
            "confidence",
            sa.String(),
            nullable=False,
            server_default="ai_detected",
        ),
        sa.Column("created_at", sa.DateTime(), nullable=False),
        sa.ForeignKeyConstraint(["claim_id"], ["claims.id"]),
        sa.ForeignKeyConstraint(["document_id"], ["documents.id"]),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index("ix_claim_evidence_id", "claim_evidence", ["id"])
    op.create_index("ix_claim_evidence_claim", "claim_evidence", ["claim_id"])
    op.create_index("ix_claim_evidence_document", "claim_evidence", ["document_id"])

    # --- user_reactions ------------------------------------------------------
    op.create_table(
        "user_reactions",
        sa.Column("id", sa.Integer(), nullable=False),
        sa.Column("document_id", sa.Integer(), nullable=False),
        sa.Column(
            "user_id",
            sa.String(),
            nullable=False,
            server_default="single_user",
        ),
        sa.Column("reaction", sa.String(), nullable=False),
        sa.Column("notes", sa.Text(), nullable=True),
        sa.Column("created_at", sa.DateTime(), nullable=False),
        sa.ForeignKeyConstraint(["document_id"], ["documents.id"]),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index("ix_user_reactions_id", "user_reactions", ["id"])
    op.create_index("ix_user_reactions_document", "user_reactions", ["document_id"])
    op.create_index("ix_user_reactions_reaction", "user_reactions", ["reaction"])

    # --- documents extensions -----------------------------------------------
    op.add_column("documents", sa.Column("key_passages", sa.JSON(), nullable=True))
    op.add_column("documents", sa.Column("cost_delta", sa.JSON(), nullable=True))


def downgrade() -> None:
    op.drop_column("documents", "cost_delta")
    op.drop_column("documents", "key_passages")

    op.drop_index("ix_user_reactions_reaction", table_name="user_reactions")
    op.drop_index("ix_user_reactions_document", table_name="user_reactions")
    op.drop_index("ix_user_reactions_id", table_name="user_reactions")
    op.drop_table("user_reactions")

    op.drop_index("ix_claim_evidence_document", table_name="claim_evidence")
    op.drop_index("ix_claim_evidence_claim", table_name="claim_evidence")
    op.drop_index("ix_claim_evidence_id", table_name="claim_evidence")
    op.drop_table("claim_evidence")

    op.drop_index("ix_claims_proceeding", table_name="claims")
    op.drop_index("ix_claims_case_status", table_name="claims")
    op.drop_index("ix_claims_case", table_name="claims")
    op.drop_index("ix_claims_source_document", table_name="claims")
    op.drop_index("ix_claims_case_id", table_name="claims")
    op.drop_index("ix_claims_id", table_name="claims")
    op.drop_table("claims")
