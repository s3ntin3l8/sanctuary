"""reset_and_sync_full_schema_v2

Revision ID: 7c6a3376821b
Revises: cc7bed04fc19
Create Date: 2026-04-17 20:47:39.836371

"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

# revision identifiers, used by Alembic.
revision: str = "7c6a3376821b"
down_revision: str | Sequence[str] | None = "cc7bed04fc19"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    """Upgrade schema."""
    with op.batch_alter_table("action_items", schema=None) as batch_op:
        # Avoid dropping ix_action_items_source_document if it might not exist
        batch_op.create_index(
            batch_op.f("ix_action_items_proceeding_id"), ["proceeding_id"], unique=False
        )
        batch_op.create_index(
            batch_op.f("ix_action_items_source_document_id"),
            ["source_document_id"],
            unique=False,
        )

    with op.batch_alter_table("claim_evidence", schema=None) as batch_op:
        batch_op.create_index(
            batch_op.f("ix_claim_evidence_claim_id"), ["claim_id"], unique=False
        )
        batch_op.create_index(
            batch_op.f("ix_claim_evidence_document_id"), ["document_id"], unique=False
        )

    with op.batch_alter_table("claims", schema=None) as batch_op:
        batch_op.create_index(
            batch_op.f("ix_claims_proceeding_id"), ["proceeding_id"], unique=False
        )
        batch_op.create_index(
            batch_op.f("ix_claims_source_document_id"),
            ["source_document_id"],
            unique=False,
        )

    with op.batch_alter_table("conversation_messages", schema=None) as batch_op:
        batch_op.create_index(
            batch_op.f("ix_conversation_messages_conversation_id"),
            ["conversation_id"],
            unique=False,
        )

    with op.batch_alter_table("document_relationships", schema=None) as batch_op:
        batch_op.create_index(
            batch_op.f("ix_document_relationships_from_document_id"),
            ["from_document_id"],
            unique=False,
        )
        batch_op.create_index(
            batch_op.f("ix_document_relationships_to_document_id"),
            ["to_document_id"],
            unique=False,
        )

    with op.batch_alter_table("documents", schema=None) as batch_op:
        batch_op.alter_column(
            "ingest_status", existing_type=sa.VARCHAR(length=10), nullable=False
        )
        batch_op.create_index(
            batch_op.f("ix_documents_ingest_batch_id"),
            ["ingest_batch_id"],
            unique=False,
        )
        batch_op.create_index(
            batch_op.f("ix_documents_proceeding_id"), ["proceeding_id"], unique=False
        )
        batch_op.create_index(
            batch_op.f("ix_documents_significance_tier"),
            ["significance_tier"],
            unique=False,
        )
        batch_op.create_foreign_key(
            "fk_documents_ingest_batch", "ingest_batches", ["ingest_batch_id"], ["id"]
        )
        batch_op.create_foreign_key(
            "fk_documents_proceeding", "proceedings", ["proceeding_id"], ["id"]
        )

    with op.batch_alter_table("ingest_batches", schema=None) as batch_op:
        batch_op.add_column(sa.Column("message_id", sa.String(), nullable=True))
        batch_op.create_index(
            batch_op.f("ix_ingest_batches_case_id"), ["case_id"], unique=False
        )
        batch_op.create_index(
            batch_op.f("ix_ingest_batches_message_id"), ["message_id"], unique=False
        )
        batch_op.create_index(
            batch_op.f("ix_ingest_batches_proceeding_id"),
            ["proceeding_id"],
            unique=False,
        )

    with op.batch_alter_table("proceedings", schema=None) as batch_op:
        batch_op.create_index(
            batch_op.f("ix_proceedings_case_id"), ["case_id"], unique=False
        )

    with op.batch_alter_table("user_reactions", schema=None) as batch_op:
        batch_op.create_index(
            batch_op.f("ix_user_reactions_document_id"), ["document_id"], unique=False
        )


def downgrade() -> None:
    """Downgrade schema."""
    with op.batch_alter_table("user_reactions", schema=None) as batch_op:
        batch_op.drop_index(batch_op.f("ix_user_reactions_document_id"))

    with op.batch_alter_table("proceedings", schema=None) as batch_op:
        batch_op.drop_index(batch_op.f("ix_proceedings_case_id"))

    with op.batch_alter_table("ingest_batches", schema=None) as batch_op:
        batch_op.drop_index(batch_op.f("ix_ingest_batches_proceeding_id"))
        batch_op.drop_index(batch_op.f("ix_ingest_batches_message_id"))
        batch_op.drop_index(batch_op.f("ix_ingest_batches_case_id"))
        batch_op.drop_column("message_id")

    with op.batch_alter_table("documents", schema=None) as batch_op:
        batch_op.drop_constraint("fk_documents_proceeding", type_="foreignkey")
        batch_op.drop_constraint("fk_documents_ingest_batch", type_="foreignkey")
        batch_op.drop_index(batch_op.f("ix_documents_significance_tier"))
        batch_op.drop_index(batch_op.f("ix_documents_proceeding_id"))
        batch_op.drop_index(batch_op.f("ix_documents_ingest_batch_id"))
        batch_op.alter_column(
            "ingest_status", existing_type=sa.VARCHAR(length=10), nullable=True
        )

    with op.batch_alter_table("document_relationships", schema=None) as batch_op:
        batch_op.drop_index(batch_op.f("ix_document_relationships_to_document_id"))
        batch_op.drop_index(batch_op.f("ix_document_relationships_from_document_id"))

    with op.batch_alter_table("conversation_messages", schema=None) as batch_op:
        batch_op.drop_index(batch_op.f("ix_conversation_messages_conversation_id"))

    with op.batch_alter_table("claims", schema=None) as batch_op:
        batch_op.drop_index(batch_op.f("ix_claims_source_document_id"))
        batch_op.drop_index(batch_op.f("ix_claims_proceeding_id"))

    with op.batch_alter_table("claim_evidence", schema=None) as batch_op:
        batch_op.drop_index(batch_op.f("ix_claim_evidence_document_id"))
        batch_op.drop_index(batch_op.f("ix_claim_evidence_claim_id"))

    with op.batch_alter_table("action_items", schema=None) as batch_op:
        batch_op.drop_index(batch_op.f("ix_action_items_source_document_id"))
        batch_op.drop_index(batch_op.f("ix_action_items_proceeding_id"))
