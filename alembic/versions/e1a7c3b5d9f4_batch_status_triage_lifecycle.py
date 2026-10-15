"""IngestBatch.status becomes the triage lifecycle only

PENDING/PROCESSING -> OPEN, COMPLETED -> CONFIRMED, FAILED -> OPEN (pipeline
progress lives per document, in Document.pipeline_state). The column is a
non-native VARCHAR with no CHECK constraint, so only the stored member names
change.

The downgrade is data-only and lossy (PENDING/FAILED cannot be restored). It
writes names only the previous release's enum knows, so run it together with
that release's code, never under this one.

Revision ID: e1a7c3b5d9f4
Revises: c8f2d4a6b1e3
Create Date: 2026-10-15 10:00:00.000000

"""

from collections.abc import Sequence

from alembic import op

# revision identifiers, used by Alembic.
revision: str = "e1a7c3b5d9f4"  # pragma: allowlist secret
down_revision: str | Sequence[str] | None = "c8f2d4a6b1e3"  # pragma: allowlist secret
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.execute(
        "UPDATE ingest_batches SET status = CASE status "
        "WHEN 'PENDING' THEN 'OPEN' "
        "WHEN 'PROCESSING' THEN 'OPEN' "
        "WHEN 'FAILED' THEN 'OPEN' "
        "WHEN 'COMPLETED' THEN 'CONFIRMED' "
        "ELSE status END"
    )


def downgrade() -> None:
    op.execute(
        "UPDATE ingest_batches SET status = CASE status "
        "WHEN 'OPEN' THEN 'PROCESSING' "
        "WHEN 'CONFIRMED' THEN 'COMPLETED' "
        "ELSE status END"
    )
