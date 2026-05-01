"""add document az_court

Revision ID: a8b9c0d1e2f3
Revises: 19a766eefe1b
Create Date: 2026-04-30 13:00:00.000000

"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

# revision identifiers, used by Alembic.
revision: str = "a8b9c0d1e2f3"
down_revision: str | Sequence[str] | None = "19a766eefe1b"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    with op.batch_alter_table("documents", schema=None) as batch_op:
        batch_op.add_column(sa.Column("az_court", sa.String(), nullable=True))
        batch_op.create_index(
            batch_op.f("ix_documents_az_court"), ["az_court"], unique=False
        )


def downgrade() -> None:
    with op.batch_alter_table("documents", schema=None) as batch_op:
        batch_op.drop_index(batch_op.f("ix_documents_az_court"))
        batch_op.drop_column("az_court")
