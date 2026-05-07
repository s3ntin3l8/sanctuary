"""claim_asserts_role_and_is_precedent

Revision ID: c11a3e5d2b7f
Revises: fef354f9b14f
Create Date: 2026-05-07 00:00:00.000000

Wave 1 of the Sharpen-Claims plan. Two additive changes:

1. `claims.is_precedent BOOLEAN NOT NULL DEFAULT 0` — flag for ⚖️ Precedent
   reaction. Independent of `status`. Renders for any claim regardless of
   originator.

2. Backfill `ClaimEvidence.role` from SUPPORTS to ASSERTS for every row that
   represents a claim's *first* assertion (i.e., role=SUPPORTS and
   document_id == claims.source_document_id). The new ASSERTS value is added
   to the application-layer enum (`app.models.enums.ClaimEvidenceRole`) — the
   DB column is `VARCHAR(14)` with no CHECK constraint, so no schema work is
   needed beyond a default-value bump.

   For any claim that lacks such a SUPPORTS row (defensive — shouldn't
   happen, but guards against handcrafted seed data), we insert a fresh
   ASSERTS row pointing at `source_document_id`.

Wave 2 will follow with the structural cross-case refactor (drop case_id,
proceeding_id, source_document_id from claims; rewrite all callers to join
through ClaimEvidence). Kept separate to land Wave 1's UX wins first.
"""

from collections.abc import Sequence
from datetime import UTC, datetime

import sqlalchemy as sa
from alembic import op

revision: str = "c11a3e5d2b7f"
down_revision: str | Sequence[str] | None = "fef354f9b14f"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    # 1. Add is_precedent column.
    with op.batch_alter_table("claims") as batch:
        batch.add_column(
            sa.Column(
                "is_precedent",
                sa.Boolean(),
                nullable=False,
                server_default=sa.false(),
            )
        )

    # 2. Backfill ASSERTS role.
    bind = op.get_bind()

    # SAEnum stores enum NAMES (uppercase) on disk, not the StrEnum values.
    # All comparisons below use the uppercase form.
    #
    # 2a. Convert the SUPPORTS evidence row that points at the claim's source
    #     document into an ASSERTS row. If multiple SUPPORTS rows exist (from
    #     idempotent re-extraction), pick the lowest id.
    bind.execute(
        sa.text(
            """
            UPDATE claim_evidence
               SET role = 'ASSERTS'
             WHERE id IN (
                SELECT MIN(ce.id)
                  FROM claim_evidence ce
                  JOIN claims c ON c.id = ce.claim_id
                 WHERE ce.document_id = c.source_document_id
                   AND ce.role = 'SUPPORTS'
              GROUP BY ce.claim_id
            )
            """
        )
    )

    # 2b. Insert a fresh ASSERTS row for any claim that has no evidence row
    #     at source_document_id (defensive — shouldn't occur in the wild).
    now = datetime.now(UTC).replace(tzinfo=None).isoformat(sep=" ", timespec="seconds")
    bind.execute(
        sa.text(
            """
            INSERT INTO claim_evidence
                (claim_id, document_id, role, excerpt, confidence, ingest_date)
            SELECT c.id, c.source_document_id, 'ASSERTS', NULL, 'AI_DETECTED', :now
              FROM claims c
              LEFT JOIN claim_evidence ce
                ON ce.claim_id = c.id
               AND ce.document_id = c.source_document_id
             WHERE ce.id IS NULL
            """
        ),
        {"now": now},
    )


def downgrade() -> None:
    bind = op.get_bind()

    # Revert ASSERTS rows back to SUPPORTS so the old code path keeps working.
    bind.execute(
        sa.text("UPDATE claim_evidence SET role = 'SUPPORTS' WHERE role = 'ASSERTS'")
    )

    with op.batch_alter_table("claims") as batch:
        batch.drop_column("is_precedent")
