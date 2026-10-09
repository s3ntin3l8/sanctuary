"""persist document confirmation; fix proposal-born claim evidence provenance

`pending_confirmation` was recomputed with confirmed=False on every refresh, so
any relationship confirm/reject or re-enrich put an already-confirmed document
back under review. The confirmation is now a fact on the row (`confirmed_at`).

Backfill: documents of a COMPLETED batch, and documents whose review_reasons no
longer carry `pending_confirmation`, count as confirmed.

Also: ClaimEvidence rows written by confirm_evidence defaulted to AI_DETECTED,
which made `contests_existing_claim` fire *after* the user confirmed the link.
They are user-confirmed by construction; fix the existing rows.

Revision ID: b4e8c2a6d0f3
Revises: a7c3e9d1b5f2
Create Date: 2026-10-09 09:00:00.000000

"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

# revision identifiers, used by Alembic.
revision: str = "b4e8c2a6d0f3"  # pragma: allowlist secret
down_revision: str | Sequence[str] | None = "a7c3e9d1b5f2"  # pragma: allowlist secret
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


BACKFILL_CONFIRMED = """
UPDATE documents SET confirmed_at = COALESCE(ingest_date, NOW())
WHERE ingest_batch_id IN (
    SELECT id FROM ingest_batches WHERE status = 'COMPLETED'
)
OR COALESCE(review_reasons::text, '') NOT LIKE '%pending_confirmation%'
"""

BACKFILL_EVIDENCE_PROVENANCE = """
UPDATE claim_evidence SET confidence = 'USER_CONFIRMED'
WHERE confidence = 'AI_DETECTED' AND EXISTS (
    SELECT 1 FROM claim_evidence_proposals p
    WHERE p.status = 'CONFIRMED'
      AND p.target_claim_id = claim_evidence.claim_id
      AND p.source_document_id = claim_evidence.document_id
      AND p.proposed_role = claim_evidence.role
)
"""


def upgrade() -> None:
    op.add_column("documents", sa.Column("confirmed_at", sa.DateTime(), nullable=True))
    op.execute(BACKFILL_CONFIRMED)
    op.execute(BACKFILL_EVIDENCE_PROVENANCE)


def downgrade() -> None:
    op.drop_column("documents", "confirmed_at")
