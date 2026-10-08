"""email threading headers on ingest_batches

Keeps In-Reply-To / References for every ingested email (they used to survive
only on attachment-less body documents) so thread_header_linker can derive
deterministic reply edges between batches.

The new RelationshipConfidence.EMAIL_HEADER value needs no DDL: enum columns
are non-native VARCHAR(14) with no CHECK constraint, and "EMAIL_HEADER" fits.

Revision ID: d9a1c3e5b7f2
Revises: d9a3f1c7e5b2
Create Date: 2026-10-08 15:00:00.000000

"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

# revision identifiers, used by Alembic.
revision: str = "d9a1c3e5b7f2"  # pragma: allowlist secret
down_revision: str | Sequence[str] | None = "d9a3f1c7e5b2"  # pragma: allowlist secret
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.add_column(
        "ingest_batches", sa.Column("in_reply_to", sa.String(), nullable=True)
    )
    op.add_column(
        "ingest_batches",
        sa.Column("thread_refs", postgresql.JSONB(), nullable=True),
    )
    op.create_index(
        op.f("ix_ingest_batches_in_reply_to"), "ingest_batches", ["in_reply_to"]
    )
    op.create_index(
        "ix_ingest_batches_thread_refs",
        "ingest_batches",
        ["thread_refs"],
        postgresql_using="gin",
    )


def downgrade() -> None:
    # The old code has no EMAIL_HEADER member; rows carrying it would fail to load.
    op.execute("DELETE FROM document_relationships WHERE confidence = 'EMAIL_HEADER'")
    op.drop_index("ix_ingest_batches_thread_refs", table_name="ingest_batches")
    op.drop_index(op.f("ix_ingest_batches_in_reply_to"), table_name="ingest_batches")
    op.drop_column("ingest_batches", "thread_refs")
    op.drop_column("ingest_batches", "in_reply_to")
