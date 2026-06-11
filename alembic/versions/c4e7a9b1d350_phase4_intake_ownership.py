"""Phase 4 — per-user intake ownership.

Adds `owner_id` to `documents` and `ingest_batches` (the per-user triage inbox)
and a unique `username` slug to `users` (names each user's scan-ingest subfolder).

All additions use native ADD COLUMN (no batch recreate, no DB-level FK) — both
`documents` and `ingest_batches` are referenced by inbound FKs, and a recreate is
the classic place SQLite migrations orphan rows. Ownership is enforced at the app
layer. Existing rows are backfilled to the bootstrap admin; usernames are derived
from the email local-part (deduped).

Revision ID: c4e7a9b1d350
Revises: b8d2f4a16c30
Create Date: 2026-05-30
"""

import re

import sqlalchemy as sa
from alembic import op

revision = "c4e7a9b1d350"
down_revision = "b8d2f4a16c30"
branch_labels = None
depends_on = None


def _slug(value: str) -> str:
    s = re.sub(r"[^a-z0-9]+", "-", (value or "").lower()).strip("-")
    return s or "user"


def upgrade() -> None:
    bind = op.get_bind()

    op.add_column("documents", sa.Column("owner_id", sa.Integer(), nullable=True))
    op.create_index("ix_documents_owner_id", "documents", ["owner_id"])

    op.add_column("ingest_batches", sa.Column("owner_id", sa.Integer(), nullable=True))
    op.create_index("ix_ingest_batches_owner_id", "ingest_batches", ["owner_id"])

    op.add_column("users", sa.Column("username", sa.String(), nullable=True))
    op.create_index("ix_users_username", "users", ["username"], unique=True)

    # Backfill usernames from email local-part, deduped.
    users = bind.execute(sa.text("SELECT id, email FROM users ORDER BY id")).fetchall()
    taken: set[str] = set()
    for uid, email in users:
        base = _slug((email or "").split("@", 1)[0])
        candidate = base
        n = 2
        while candidate in taken:
            candidate = f"{base}-{n}"
            n += 1
        taken.add(candidate)
        bind.execute(
            sa.text("UPDATE users SET username = :u WHERE id = :id"),
            {"u": candidate, "id": uid},
        )

    # Backfill existing batches/documents to the bootstrap admin so they remain
    # visible in that admin's triage inbox.
    admin_id = bind.execute(
        # role is the UserRole *name* ('ADMIN'), matching how the ORM persists it.
        sa.text("SELECT id FROM users WHERE role = 'ADMIN' ORDER BY id LIMIT 1")
    ).scalar()
    if admin_id is not None:
        bind.execute(
            sa.text("UPDATE documents SET owner_id = :id WHERE owner_id IS NULL"),
            {"id": admin_id},
        )
        bind.execute(
            sa.text("UPDATE ingest_batches SET owner_id = :id WHERE owner_id IS NULL"),
            {"id": admin_id},
        )


def downgrade() -> None:
    op.drop_index("ix_users_username", table_name="users")
    op.drop_column("users", "username")
    op.drop_index("ix_ingest_batches_owner_id", table_name="ingest_batches")
    op.drop_column("ingest_batches", "owner_id")
    op.drop_index("ix_documents_owner_id", table_name="documents")
    op.drop_column("documents", "owner_id")
