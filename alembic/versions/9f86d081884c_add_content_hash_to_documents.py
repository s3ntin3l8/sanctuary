"""add content_hash to documents

Revision ID: 9f86d081884c
Revises: 698c5f71bf23
Create Date: 2026-04-05 03:00:00.000000
"""

from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa

revision: str = "9f86d081884c"
down_revision: Union[str, Sequence[str], None] = "698c5f71bf23"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    """Add content_hash column to documents table (idempotent)."""
    conn = op.get_bind()
    existing_tables = conn.execute(
        sa.text("SELECT name FROM sqlite_master WHERE type='table'")
    ).fetchall()
    existing_tables = {row[0] for row in existing_tables}

    if "documents" in existing_tables:
        doc_cols = conn.execute(sa.text("PRAGMA table_info(documents)")).fetchall()
        doc_col_names = {row[1] for row in doc_cols}
        if "content_hash" not in doc_col_names:
            op.add_column(
                "documents",
                sa.Column("content_hash", sa.String(64), nullable=True),
            )

        # Create index if it doesn't already exist
        indexes = conn.execute(
            sa.text("SELECT name FROM sqlite_master WHERE type='index'")
        ).fetchall()
        index_names = {row[0] for row in indexes}
        if "ix_documents_content_hash" not in index_names:
            op.create_index(
                op.f("ix_documents_content_hash"),
                "documents",
                ["content_hash"],
                unique=False,
            )


def downgrade() -> None:
    """Remove content_hash column and index."""
    op.drop_index(op.f("ix_documents_content_hash"), table_name="documents")
    with op.batch_alter_table("documents") as batch_op:
        batch_op.drop_column("content_hash")
