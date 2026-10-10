"""drop duplicate and primary-key-redundant indexes

Two ORM habits created indexes Postgres then maintained twice:

- a column with ``index=True`` *and* an explicit ``Index(...)`` on the same
  column (``ix_documents_proceeding_id`` + ``ix_documents_proceeding``, ...);
  the explicit twin is dropped, the column index stays.
- ``primary_key=True, index=True`` on every ``id``: ``ix_<table>_id`` is a
  second, non-unique btree beside the primary key's own unique index.

Revision ID: b7e3a9c1d5f2
Revises: c9d1e5a7b3f2
Create Date: 2026-10-10 20:00:00.000000

"""

from collections.abc import Sequence

from alembic import op

# revision identifiers, used by Alembic.
revision: str = "b7e3a9c1d5f2"  # pragma: allowlist secret
down_revision: str | Sequence[str] | None = "c9d1e5a7b3f2"  # pragma: allowlist secret
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

# (index name, table, column) — explicit twins of an existing ``ix_<table>_<column>``.
_TWINS = [
    ("ix_action_items_proceeding", "action_items", "proceeding_id"),
    ("ix_batch_sub_groups_batch", "batch_sub_groups", "batch_id"),
    ("ix_claim_evidence_claim", "claim_evidence", "claim_id"),
    ("ix_claim_evidence_document", "claim_evidence", "document_id"),
    (
        "ix_conversation_messages_conversation",
        "conversation_messages",
        "conversation_id",
    ),
    ("ix_document_pins_document", "document_pins", "document_id"),
    ("ix_document_relationships_from", "document_relationships", "from_document_id"),
    ("ix_document_relationships_to", "document_relationships", "to_document_id"),
    ("ix_documents_sub_group", "documents", "sub_group_id"),
    ("ix_documents_ingest_batch", "documents", "ingest_batch_id"),
    ("ix_documents_proceeding", "documents", "proceeding_id"),
    ("ix_documents_significance", "documents", "significance_tier"),
    ("ix_ingest_batches_case", "ingest_batches", "case_id"),
    ("ix_proceedings_case", "proceedings", "case_id"),
    ("ix_user_reactions_document", "user_reactions", "document_id"),
]

_PK_TABLES = [
    "action_items",
    "batch_sub_groups",
    "case_shares",
    "cases",
    "claim_evidence",
    "claim_evidence_proposals",
    "claim_merge_proposals",
    "claims",
    "conversation_messages",
    "conversations",
    "cost_signals",
    "document_chunks",
    "document_pins",
    "document_relationships",
    "documents",
    "entities",
    "ingest_batches",
    "legal_costs",
    "proceedings",
    "user_reactions",
    "users",
]


def upgrade() -> None:
    for name, _table, _column in _TWINS:
        op.execute(f'DROP INDEX IF EXISTS "{name}"')
    for table in _PK_TABLES:
        op.execute(f'DROP INDEX IF EXISTS "ix_{table}_id"')


def downgrade() -> None:
    # Exact inverse, for a rollback. It rebuilds plain (non-CONCURRENT) indexes,
    # which block writes to the table while they build: schedule a downgrade.
    # Until the next upgrade the schema carries indexes the models no longer
    # declare; re-running upgrade is the supported way back.
    for table in _PK_TABLES:
        op.execute(f'CREATE INDEX IF NOT EXISTS "ix_{table}_id" ON "{table}" ("id")')
    for name, table, column in _TWINS:
        op.execute(f'CREATE INDEX IF NOT EXISTS "{name}" ON "{table}" ("{column}")')
