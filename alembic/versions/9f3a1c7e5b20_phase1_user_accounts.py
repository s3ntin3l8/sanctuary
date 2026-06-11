"""Phase 1 — user accounts: users + app_settings, per-user ownership.

Creates the ``users`` and ``app_settings`` tables, seeds a bootstrap admin,
converts the ``single_user`` string stubs to integer FKs pointing at that admin,
splits global keys out of ``user_settings`` into ``app_settings``, and adds
``cases.owner_id`` + ``conversations.user_id`` ownership columns.

Chains after c4d9e2f1a6b3 (the stray-index reconcile that repaired the fresh-DB
migration chain), so ``alembic upgrade head`` runs cleanly on a new database.

Revision ID: 9f3a1c7e5b20
Revises: c4d9e2f1a6b3
Create Date: 2026-05-29
"""

import os

import sqlalchemy as sa
from alembic import op
from sqlalchemy import inspect as sa_inspect

revision = "9f3a1c7e5b20"
down_revision = "c4d9e2f1a6b3"
branch_labels = None
depends_on = None


_GLOBAL_KEYS = (
    "ai",
    "reindex_job",
    "dedup_jobs",
    "ingestion",
    "party_identity",
    "timezone",
)
# NOTE: gmail_* keys are intentionally NOT global — Gmail is connected per-user,
# so those keys stay in each user's UserSettings row.


def upgrade() -> None:
    bind = op.get_bind()

    # 1. New tables ---------------------------------------------------------
    op.create_table(
        "users",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column("email", sa.String(), nullable=False),
        sa.Column("password_hash", sa.String(), nullable=True),
        sa.Column("display_name", sa.String(), nullable=True),
        sa.Column("role", sa.String(), nullable=False, server_default="user"),
        sa.Column("is_active", sa.Boolean(), nullable=False, server_default=sa.true()),
        sa.Column("token_version", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("oidc_subject", sa.String(), nullable=True),
        sa.Column("oidc_issuer", sa.String(), nullable=True),
        sa.Column("created_at", sa.DateTime(), nullable=False),
        sa.Column("updated_at", sa.DateTime(), nullable=False),
        sa.Column("last_login_at", sa.DateTime(), nullable=True),
        sa.UniqueConstraint("email", name="uq_users_email"),
        sa.UniqueConstraint(
            "oidc_issuer", "oidc_subject", name="uq_users_oidc_identity"
        ),
    )
    op.create_index("ix_users_email", "users", ["email"], unique=True)
    op.create_index("ix_users_is_active", "users", ["is_active"])
    op.create_index("ix_users_oidc_subject", "users", ["oidc_subject"])

    op.create_table(
        "app_settings",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column("settings_json", sa.JSON(), nullable=True),
        sa.Column("updated_at", sa.DateTime(), nullable=True),
    )

    # 2. Bootstrap admin ----------------------------------------------------
    admin_email = (
        (os.getenv("BOOTSTRAP_ADMIN_EMAIL", "") or "admin@localhost").strip().lower()
    )
    bind.execute(
        sa.text(
            # role stores the UserRole *name* (SAEnum persists names), so this
            # must be 'ADMIN', not the lowercase value 'admin'.
            "INSERT INTO users (email, role, is_active, token_version, "
            "created_at, updated_at) VALUES (:email, 'ADMIN', 1, 0, "
            "CURRENT_TIMESTAMP, CURRENT_TIMESTAMP)"
        ),
        {"email": admin_email},
    )
    admin_id = bind.execute(
        sa.text("SELECT id FROM users WHERE email = :email"), {"email": admin_email}
    ).scalar_one()

    # 3. Split global keys out of the legacy single user_settings row -------
    legacy = bind.execute(
        sa.text("SELECT settings_json FROM user_settings LIMIT 1")
    ).fetchone()
    if legacy and legacy[0]:
        import json

        data = legacy[0] if isinstance(legacy[0], dict) else json.loads(legacy[0])
        global_part = {k: data[k] for k in _GLOBAL_KEYS if k in data}
        bind.execute(
            sa.text(
                "INSERT INTO app_settings (settings_json, updated_at) "
                "VALUES (:j, CURRENT_TIMESTAMP)"
            ),
            {"j": json.dumps(global_part)},
        )
        per_user_part = {k: v for k, v in data.items() if k not in _GLOBAL_KEYS}
        bind.execute(
            sa.text("UPDATE user_settings SET settings_json = :j"),
            {"j": json.dumps(per_user_part)},
        )
    else:
        bind.execute(
            sa.text(
                "INSERT INTO app_settings (settings_json, updated_at) "
                "VALUES ('{}', CURRENT_TIMESTAMP)"
            )
        )

    # 4. Repoint the 'single_user' string stubs to the admin's integer id ---
    for table in ("user_reactions", "document_pins", "user_settings"):
        bind.execute(sa.text(f"UPDATE {table} SET user_id = :uid"), {"uid": admin_id})

    # 5. Convert column types to integer FKs (SQLite → batch recreate) ------
    with op.batch_alter_table("user_reactions") as batch:
        batch.alter_column(
            "user_id", existing_type=sa.String(), type_=sa.Integer(), nullable=False
        )
        batch.create_foreign_key(
            "fk_user_reactions_user", "users", ["user_id"], ["id"], ondelete="CASCADE"
        )
        batch.create_index("ix_user_reactions_user", ["user_id"])

    with op.batch_alter_table("document_pins") as batch:
        batch.alter_column(
            "user_id", existing_type=sa.String(), type_=sa.Integer(), nullable=False
        )
        batch.create_foreign_key(
            "fk_document_pins_user", "users", ["user_id"], ["id"], ondelete="CASCADE"
        )
        batch.create_index("ix_document_pins_user", ["user_id"])

    with op.batch_alter_table("user_settings") as batch:
        batch.alter_column(
            "user_id", existing_type=sa.String(), type_=sa.Integer(), nullable=False
        )
        batch.create_foreign_key(
            "fk_user_settings_user", "users", ["user_id"], ["id"], ondelete="CASCADE"
        )
        batch.create_index("ix_user_settings_user", ["user_id"])

    # 6. AuditLog: actor (string) → actor_user_id (FK) + actor_label --------
    with op.batch_alter_table("audit_logs") as batch:
        batch.add_column(sa.Column("actor_user_id", sa.Integer(), nullable=True))
        batch.add_column(sa.Column("actor_label", sa.String(), nullable=True))
    bind.execute(sa.text("UPDATE audit_logs SET actor_label = 'system'"))
    with op.batch_alter_table("audit_logs") as batch:
        batch.create_foreign_key(
            "fk_audit_logs_actor_user",
            "users",
            ["actor_user_id"],
            ["id"],
            ondelete="SET NULL",
        )
        batch.create_index("ix_audit_logs_actor_user", ["actor_user_id"])
        batch.drop_column("actor")

    # 7. Ownership columns --------------------------------------------------
    # IMPORTANT: use a plain ADD COLUMN (native in SQLite — no table recreate)
    # rather than batch_alter_table here. `cases` is referenced by inbound FKs
    # from proceedings/documents/legal_costs/entities/cost_signals/case_shares,
    # and a batch recreate of a heavily-referenced table is the classic place
    # SQLite migrations orphan rows. We deliberately omit the DB-level FK
    # constraint on owner_id/user_id (which WOULD force a recreate) — ownership
    # is enforced at the application layer (access_service); the ORM keeps the
    # relationship metadata.
    op.add_column("cases", sa.Column("owner_id", sa.Integer(), nullable=True))
    op.create_index("ix_cases_owner_id", "cases", ["owner_id"])

    op.add_column("conversations", sa.Column("user_id", sa.Integer(), nullable=True))
    op.create_index("ix_conversations_user_id", "conversations", ["user_id"])

    # Backfill existing cases to the bootstrap admin so they remain visible.
    insp = sa_inspect(bind)
    if "cases" in insp.get_table_names():
        bind.execute(
            sa.text("UPDATE cases SET owner_id = :uid WHERE owner_id IS NULL"),
            {"uid": admin_id},
        )


def downgrade() -> None:
    op.drop_index("ix_conversations_user_id", table_name="conversations")
    op.drop_column("conversations", "user_id")
    op.drop_index("ix_cases_owner_id", table_name="cases")
    op.drop_column("cases", "owner_id")
    with op.batch_alter_table("audit_logs") as batch:
        batch.add_column(
            sa.Column(
                "actor", sa.String(), nullable=False, server_default="single_user"
            )
        )
        batch.drop_index("ix_audit_logs_actor_user")
        batch.drop_column("actor_label")
        batch.drop_column("actor_user_id")
    for table, ix, fk in (
        ("user_settings", "ix_user_settings_user", "fk_user_settings_user"),
        ("document_pins", "ix_document_pins_user", "fk_document_pins_user"),
        ("user_reactions", "ix_user_reactions_user", "fk_user_reactions_user"),
    ):
        with op.batch_alter_table(table) as batch:
            batch.drop_index(ix)
            batch.alter_column(
                "user_id", existing_type=sa.Integer(), type_=sa.String(), nullable=False
            )
    op.drop_table("app_settings")
    op.drop_index("ix_users_oidc_subject", table_name="users")
    op.drop_index("ix_users_is_active", table_name="users")
    op.drop_index("ix_users_email", table_name="users")
    op.drop_table("users")
