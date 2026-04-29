"""add_unique_constraint_document_relationships

Revision ID: c3d4e5f6a7b9
Revises: b2c3d4e5f6a8
Create Date: 2026-04-29 00:00:00.000000

Wave 3 #17: prevent AI re-runs from accumulating duplicate edges.

`DocumentRelationshipRepository.link()` is now upsert-style (returns the
existing row when the (from, to, type) triple already exists). Add the
matching DB-level UniqueConstraint as belt-and-braces — any future code path
that bypasses the repository will get an IntegrityError instead of a silent
duplicate.

The pre-step DELETE defensively drops any existing duplicates (live DB has 0
today; query returns 0 rows). Without it the unique-index creation would
fail on real data with duplicates.
"""

from collections.abc import Sequence

from alembic import op

revision: str = "c3d4e5f6a7b9"
down_revision: str | Sequence[str] | None = "b2c3d4e5f6a8"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    # Drop duplicates first — keep the lowest id per (from, to, type) triple.
    op.execute(
        """
        DELETE FROM document_relationships
        WHERE id NOT IN (
            SELECT MIN(id)
            FROM document_relationships
            GROUP BY from_document_id, to_document_id, relationship_type
        )
        """
    )
    op.create_index(
        "uq_document_relationships_edge",
        "document_relationships",
        ["from_document_id", "to_document_id", "relationship_type"],
        unique=True,
    )


def downgrade() -> None:
    op.drop_index("uq_document_relationships_edge", table_name="document_relationships")
