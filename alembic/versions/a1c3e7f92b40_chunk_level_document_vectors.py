"""chunk_level_document_vectors

Revision ID: a1c3e7f92b40
Revises: 60a19357414a
Create Date: 2026-07-11 00:00:00.000000

Replaces whole-document embeddings with passage-level (chunk) embeddings.

`document_vectors` held exactly one vector per document — the entire
extracted text (up to ~22k chars) collapsed into a single 768-dim vector.
Retrieval could point at the right *document* but never the right
*passage*: the one sentence that matters got averaged away with dozens of
pages of boilerplate.

This migration:
1. Drops `document_vectors` (vec0). No migration path exists from
   doc-level to chunk-level vectors — a full reindex (Settings → AI →
   Rebuild Index, or `reindex_all_embeddings_task`) is required after
   upgrading. Test data only, per CLAUDE.md — no backward-compat shim.
2. Creates `document_chunks` — a real table, one row per section-level
   slice of a document's extracted text.
3. Creates `document_chunk_vectors` (vec0) — one vector per chunk, keyed
   by `document_chunks.id`. Dimension matches the live `document_vectors`
   dim (read at migration time to avoid drift), mirroring the
   `claim_vectors` migration's approach.
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "a1c3e7f92b40"
down_revision: str | Sequence[str] | None = "60a19357414a"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    bind = op.get_bind()
    row = bind.execute(
        sa.text("SELECT sql FROM sqlite_master WHERE name = 'document_vectors'")
    ).fetchone()
    embed_dim = 768  # nomic-embed-text default fallback
    if row and row[0]:
        import re

        m = re.search(r"embedding\s+float\s*\[\s*(\d+)\s*\]", row[0], re.IGNORECASE)
        if m:
            embed_dim = int(m.group(1))

    op.execute("DROP TABLE IF EXISTS document_vectors")

    op.create_table(
        "document_chunks",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column(
            "document_id",
            sa.Integer(),
            sa.ForeignKey("documents.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column("chunk_index", sa.Integer(), nullable=False),
        sa.Column("text", sa.Text(), nullable=False),
        sa.Column(
            "ingest_date",
            sa.DateTime(),
            nullable=False,
            server_default=sa.func.current_timestamp(),
        ),
    )
    op.create_index("ix_document_chunks_document", "document_chunks", ["document_id"])

    op.execute(
        f"CREATE VIRTUAL TABLE IF NOT EXISTS document_chunk_vectors "
        f"USING vec0(chunk_id INTEGER PRIMARY KEY, embedding float[{embed_dim}])"
    )


def downgrade() -> None:
    bind = op.get_bind()
    row = bind.execute(
        sa.text("SELECT sql FROM sqlite_master WHERE name = 'document_chunk_vectors'")
    ).fetchone()
    embed_dim = 768
    if row and row[0]:
        import re

        m = re.search(r"embedding\s+float\s*\[\s*(\d+)\s*\]", row[0], re.IGNORECASE)
        if m:
            embed_dim = int(m.group(1))

    op.execute("DROP TABLE IF EXISTS document_chunk_vectors")
    op.drop_index("ix_document_chunks_document", table_name="document_chunks")
    op.drop_table("document_chunks")

    op.execute(
        f"CREATE VIRTUAL TABLE IF NOT EXISTS document_vectors "
        f"USING vec0(document_id INTEGER PRIMARY KEY, embedding float[{embed_dim}])"
    )
