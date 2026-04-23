"""add_document_vectors_and_drop_content_embedding

Revision ID: 833f6caa97c7
Revises: 7ffe9c4d9e2e
Create Date: 2026-04-22 09:30:31.233834

"""

import os
from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

# revision identifiers, used by Alembic.
revision: str = "833f6caa97c7"
down_revision: str | Sequence[str] | None = "7ffe9c4d9e2e"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

EMBED_DIM = int(os.getenv("AI_EMBED_DIM", "768"))


def upgrade() -> None:
    # Create the vec0 virtual table for document embeddings.
    op.execute(
        f"CREATE VIRTUAL TABLE IF NOT EXISTS document_vectors "
        f"USING vec0(document_id INTEGER PRIMARY KEY, embedding float[{EMBED_DIM}])"
    )

    # Drop the legacy JSON-text column now that we have a proper vector table.
    with op.batch_alter_table("documents") as batch_op:
        batch_op.drop_column("content_embedding")


def downgrade() -> None:
    op.execute("DROP TABLE IF EXISTS document_vectors")

    with op.batch_alter_table("documents") as batch_op:
        batch_op.add_column(sa.Column("content_embedding", sa.Text(), nullable=True))
