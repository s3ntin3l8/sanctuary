"""add encloses relationship type

Revision ID: dbfeb8b8bc2f
Revises: a3f1e9b2d4c7
Create Date: 2026-05-14 16:15:03.091853

"""

from collections.abc import Sequence

from alembic import op

# revision identifiers, used by Alembic.
revision: str = "dbfeb8b8bc2f"
down_revision: str | Sequence[str] | None = "a3f1e9b2d4c7"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    """Add ENCLOSES to RelationshipType.

    Live schema stores `relationship_type` as VARCHAR with no CHECK
    constraint (the SAEnum is enforced at the application layer only),
    so no DDL change is required to accept the new value.
    """
    pass


def downgrade() -> None:
    """Drop any rows that use the new enum value before reverting."""
    op.execute(
        "DELETE FROM document_relationships WHERE relationship_type = 'encloses'"
    )
