"""add_document_pins

Revision ID: ef3a70ca6ad7
Revises: ccfc2ccea207
Create Date: 2026-04-20 21:35:40.392980

"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

# revision identifiers, used by Alembic.
revision: str = "ef3a70ca6ad7"
down_revision: str | Sequence[str] | None = "ccfc2ccea207"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "document_pins",
        sa.Column("id", sa.Integer(), primary_key=True, index=True),
        sa.Column(
            "document_id",
            sa.Integer(),
            sa.ForeignKey("documents.id"),
            nullable=False,
            index=True,
        ),
        sa.Column("passage_id", sa.String(12), nullable=False),
        sa.Column("note", sa.Text(), nullable=True),
        sa.Column("user_id", sa.String(), nullable=False, server_default="single_user"),
        sa.Column("created_at", sa.DateTime(), nullable=False),
        sa.Column("updated_at", sa.DateTime(), nullable=False),
    )
    op.create_index("ix_document_pins_document", "document_pins", ["document_id"])
    op.create_index("ix_document_pins_passage", "document_pins", ["passage_id"])


def downgrade() -> None:
    op.drop_index("ix_document_pins_passage", table_name="document_pins")
    op.drop_index("ix_document_pins_document", table_name="document_pins")
    op.drop_table("document_pins")
