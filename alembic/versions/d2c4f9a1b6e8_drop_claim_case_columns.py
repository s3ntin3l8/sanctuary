"""drop_claim_case_columns

Revision ID: d2c4f9a1b6e8
Revises: c11a3e5d2b7f
Create Date: 2026-05-08 00:00:00.000000

Wave 2A: claims become global (cross-case). The case_id, proceeding_id, and
source_document_id columns drop off the `claims` table. Case context now
lives entirely on `ClaimEvidence`: every claim has at least one
`ClaimEvidence(role=ASSERTS)` row whose document carries the case_id, and
queries that need case scope join through that.

Why: a claim like "sole custody of the children belongs to the creditor"
that's established in a family-court matter should be reusable as evidence
in a partition-auction matter. Forcing claims to live in exactly one case
prevented that. Cross-case dedup (Wave 2B) also depends on global claim
identity.

Precondition (verified live): every existing claim has at least one
ASSERTS evidence row pointing at its former source_document_id. Wave 1's
migration c11a3e5d2b7f did this backfill, and a fresh check before this
migration ran reported 156/156 claims covered.

This migration is one-way. Pre-release; downgrade not implemented.
"""

from collections.abc import Sequence

import sqlalchemy as sa  # noqa: F401  (alembic helper imports it implicitly)
from alembic import op
from sqlalchemy import inspect as sa_inspect

revision: str = "d2c4f9a1b6e8"
down_revision: str | Sequence[str] | None = "c11a3e5d2b7f"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    # Sanity check before destructive change: every claim must have an
    # ASSERTS evidence row, otherwise dropping source_document_id will lose
    # provenance entirely. This is belt-and-braces — Wave 1 already ran the
    # backfill — but the cost of a SELECT here is trivial against the cost
    # of recovering from a bad migration.
    bind = op.get_bind()
    orphans = bind.execute(
        sa.text(
            """
            SELECT COUNT(*)
              FROM claims c
              LEFT JOIN claim_evidence ce
                ON ce.claim_id = c.id
               AND ce.role = 'ASSERTS'
             WHERE ce.id IS NULL
            """
        )
    ).scalar()
    if orphans:
        raise RuntimeError(
            f"Refusing to migrate: {orphans} claim(s) have no ASSERTS evidence row. "
            f"Re-run Wave 1's c11a3e5d2b7f backfill first."
        )

    # Guard: only drop indexes that actually exist. Migration d4e5f6a7b8c1
    # recreated the claims table from sqlite_master without preserving indexes
    # (a since-fixed bug), so these indexes may be absent on databases that
    # ran through that migration before the fix landed.
    existing_idx = {idx["name"] for idx in sa_inspect(bind).get_indexes("claims")}

    # Drop indexes that reference the columns being dropped FIRST. Without
    # this, batch_alter_table reflects the live schema, recreates the same
    # indexes, and fails on CREATE INDEX … (proceeding_id) because the
    # column is gone.
    with op.batch_alter_table("claims") as batch:
        for idx_name in (
            "ix_claims_case",
            "ix_claims_case_status",
            "ix_claims_proceeding",
            "ix_claims_case_id",
            "ix_claims_proceeding_id",
            "ix_claims_source_document_id",
        ):
            if idx_name in existing_idx:
                batch.drop_index(idx_name)
        batch.drop_column("case_id")
        batch.drop_column("proceeding_id")
        batch.drop_column("source_document_id")
        # Truth-map queries filter by status before joining through
        # ClaimEvidence → Document for case scope. Index status alone.
        batch.create_index("ix_claims_status", ["status"])


def downgrade() -> None:
    raise NotImplementedError(
        "Wave 2A is one-way. Downgrade would require reconstructing case_id / "
        "proceeding_id / source_document_id from ClaimEvidence ASSERTS rows; "
        "pre-release with test data, not worth the implementation cost."
    )
