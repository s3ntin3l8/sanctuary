"""remember rejected document relationships

A rejected edge used to be hard-deleted with no memory, so the next detection
run (AI re-enrich, header re-link) wrote it again. This table records the
rejection; insert_edge_if_absent consults it.

Revision ID: e1b5d9a3c7f4
Revises: d9a1c3e5b7f2
Create Date: 2026-10-08 18:30:00.000000

"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

# revision identifiers, used by Alembic.
revision: str = "e1b5d9a3c7f4"  # pragma: allowlist secret
down_revision: str | Sequence[str] | None = "d9a1c3e5b7f2"  # pragma: allowlist secret
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "rejected_relationships",
        sa.Column("id", sa.Integer(), nullable=False),
        sa.Column("from_document_id", sa.Integer(), nullable=False),
        sa.Column("to_document_id", sa.Integer(), nullable=False),
        sa.Column(
            "relationship_type",
            sa.Enum(
                "REPLIES_TO",
                "REFERENCES",
                "ATTACHES_AS_PROOF",
                "SUPERSEDES",
                "CITED_BY",
                "ENCLOSES",
                name="relationshiptype",
                native_enum=False,
            ),
            nullable=False,
        ),
        sa.Column("rejected_at", sa.DateTime(timezone=True), nullable=False),
        sa.ForeignKeyConstraint(
            ["from_document_id"], ["documents.id"], ondelete="CASCADE"
        ),
        sa.ForeignKeyConstraint(
            ["to_document_id"], ["documents.id"], ondelete="CASCADE"
        ),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint(
            "from_document_id",
            "to_document_id",
            "relationship_type",
            name="uq_rejected_relationships_edge",
        ),
    )
    op.create_index(
        op.f("ix_rejected_relationships_from_document_id"),
        "rejected_relationships",
        ["from_document_id"],
    )
    op.create_index(
        op.f("ix_rejected_relationships_to_document_id"),
        "rejected_relationships",
        ["to_document_id"],
    )


def downgrade() -> None:
    op.drop_index(
        op.f("ix_rejected_relationships_to_document_id"),
        table_name="rejected_relationships",
    )
    op.drop_index(
        op.f("ix_rejected_relationships_from_document_id"),
        table_name="rejected_relationships",
    )
    op.drop_table("rejected_relationships")
