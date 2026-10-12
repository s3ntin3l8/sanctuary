"""one claim-evidence link per (claim, document)

The extractor and the confirm path keyed duplicate checks on (claim, document,
role), so one document could hold a CONTESTS and a REFUTES link to the same
claim over overlapping quotes. A document now holds one link per claim; the
stronger role wins. Pending proposals get the same rule as a partial unique
index.

Revision ID: c4f8a2d6e1b9
Revises: b7e3a9c1d5f2
Create Date: 2026-10-11 12:00:00.000000

"""

from collections.abc import Sequence

from alembic import op

# revision identifiers, used by Alembic.
revision: str = "c4f8a2d6e1b9"  # pragma: allowlist secret
down_revision: str | Sequence[str] | None = "b7e3a9c1d5f2"  # pragma: allowlist secret
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

# Enum columns store member *names*. Mirrors EVIDENCE_ROLE_STRENGTH.
_STRENGTH = """CASE {col}
        WHEN 'ASSERTS' THEN 5 WHEN 'REFUTES' THEN 4 WHEN 'CONTESTS' THEN 3
        WHEN 'SUPPORTS' THEN 2 ELSE 1 END"""


# Keep the user-confirmed row, then the strongest role, then the newest.
DEDUPE_EVIDENCE = f"""
DELETE FROM claim_evidence WHERE id IN (
    SELECT id FROM (
        SELECT id, ROW_NUMBER() OVER (
            PARTITION BY claim_id, document_id
            ORDER BY (confidence <> 'AI_DETECTED') DESC,
                     {_STRENGTH.format(col="role")} DESC,
                     id DESC
        ) AS rn
        FROM claim_evidence
    ) ranked WHERE rn > 1
)
"""

DEDUPE_PENDING_PROPOSALS = f"""
DELETE FROM claim_evidence_proposals WHERE id IN (
    SELECT id FROM (
        SELECT id, ROW_NUMBER() OVER (
            PARTITION BY target_claim_id, source_document_id
            ORDER BY {_STRENGTH.format(col="proposed_role")} DESC, id DESC
        ) AS rn
        FROM claim_evidence_proposals
        WHERE status = 'PENDING'
    ) ranked WHERE rn > 1
)
"""


def upgrade() -> None:
    op.execute(DEDUPE_EVIDENCE)
    op.execute(DEDUPE_PENDING_PROPOSALS)
    op.create_unique_constraint(
        "uq_claim_evidence_claim_document",
        "claim_evidence",
        ["claim_id", "document_id"],
    )
    op.create_index(
        "uq_claim_evidence_proposals_pending_pair",
        "claim_evidence_proposals",
        ["target_claim_id", "source_document_id"],
        unique=True,
        postgresql_where="status = 'PENDING'",
    )


def downgrade() -> None:
    op.drop_index(
        "uq_claim_evidence_proposals_pending_pair",
        table_name="claim_evidence_proposals",
    )
    op.drop_constraint(
        "uq_claim_evidence_claim_document", "claim_evidence", type_="unique"
    )
