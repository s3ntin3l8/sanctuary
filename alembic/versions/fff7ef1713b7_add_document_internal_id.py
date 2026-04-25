"""add document internal_id

Revision ID: fff7ef1713b7
Revises: 20260424162237
Create Date: 2026-04-25 00:12:08.658937

"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

# revision identifiers, used by Alembic.
revision: str = "fff7ef1713b7"
down_revision: str | Sequence[str] | None = "20260424162237"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    with op.batch_alter_table("documents", schema=None) as batch_op:
        batch_op.add_column(sa.Column("internal_id", sa.String(), nullable=True))
        batch_op.create_index(
            batch_op.f("ix_documents_internal_id"), ["internal_id"], unique=False
        )


def downgrade() -> None:
    with op.batch_alter_table("documents", schema=None) as batch_op:
        batch_op.drop_index(batch_op.f("ix_documents_internal_id"))
        batch_op.drop_column("internal_id")
