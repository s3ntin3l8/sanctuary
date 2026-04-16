"""add_meta_to_documents

Revision ID: 711ce72418b0
Revises: composite_indexes_001
Create Date: 2026-04-16 12:22:54.399731

"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

# revision identifiers, used by Alembic.
revision: str = "711ce72418b0"
down_revision: str | Sequence[str] | None = "composite_indexes_001"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    """Upgrade schema."""
    # Add column meta to documents
    op.add_column("documents", sa.Column("meta", sa.JSON(), nullable=True))


def downgrade() -> None:
    """Downgrade schema."""
    op.drop_column("documents", "meta")
