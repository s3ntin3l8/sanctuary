"""Preserve email sources, attachment outcomes and receipt provenance."""

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects.postgresql import JSONB

revision = "c2e6a4b8d013"  # pragma: allowlist secret
down_revision = "a2c6e8f1b3d5"  # pragma: allowlist secret
branch_labels = None
depends_on = None


def upgrade():
    for name in ("gmail_id", "gmail_thread_id", "received_source"):
        op.add_column("ingest_batches", sa.Column(name, sa.String(), nullable=True))
    for name in ("email_sources", "source_review_reasons"):
        op.add_column("ingest_batches", sa.Column(name, JSONB(), nullable=True))
    op.add_column(
        "ingest_batches",
        sa.Column(
            "source_review_acknowledged",
            sa.Boolean(),
            nullable=False,
            server_default=sa.false(),
        ),
    )
    op.create_unique_constraint(
        "uq_ingest_batches_owner_gmail_id", "ingest_batches", ["owner_id", "gmail_id"]
    )
    # Preserve old transport evidence until the deliberate workspace replay.
    op.execute("""UPDATE ingest_batches SET email_sources = jsonb_build_array(jsonb_build_object(
        'id', '0', 'parent_id', NULL, 'sender', coalesce(sender_email, ''),
        'subject', coalesce(subject, ''), 'message_id', coalesce(message_id, ''),
        'email_note', coalesce(email_note, ''), 'attachment_manifest', coalesce(attachment_manifest, '[]'::jsonb),
        'parts', '[]'::jsonb)), received_source = 'legacy_unknown',
        source_review_reasons = '["legacy_source_identity"]'::jsonb
        WHERE source_type = 'EMAIL'""")
    op.drop_column("ingest_batches", "email_note")
    op.drop_column("ingest_batches", "attachment_manifest")
    for name in ("email_source_id", "email_part_id"):
        op.add_column("documents", sa.Column(name, sa.String(), nullable=True))
    op.execute("""UPDATE documents SET email_source_id = '0'
        FROM ingest_batches b WHERE documents.ingest_batch_id = b.id
        AND b.source_type = 'EMAIL'""")


def downgrade():
    op.add_column("ingest_batches", sa.Column("email_note", sa.Text(), nullable=True))
    op.add_column(
        "ingest_batches", sa.Column("attachment_manifest", JSONB(), nullable=True)
    )
    op.execute("""UPDATE ingest_batches SET email_note = email_sources->0->>'email_note',
        attachment_manifest = email_sources->0->'attachment_manifest' WHERE email_sources IS NOT NULL""")
    for name in ("email_source_id", "email_part_id"):
        op.drop_column("documents", name)
    op.drop_constraint(
        "uq_ingest_batches_owner_gmail_id", "ingest_batches", type_="unique"
    )
    for name in (
        "gmail_id",
        "gmail_thread_id",
        "received_source",
        "email_sources",
        "source_review_reasons",
        "source_review_acknowledged",
    ):
        op.drop_column("ingest_batches", name)
