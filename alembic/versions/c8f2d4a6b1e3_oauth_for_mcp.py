"""OAuth 2.1 for the MCP endpoint: clients, codes, grants, token kind

Revision ID: c8f2d4a6b1e3
Revises: b6e4a2c9d1f7
Create Date: 2026-10-12 18:00:00.000000

"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

# revision identifiers, used by Alembic.
revision: str = "c8f2d4a6b1e3"  # pragma: allowlist secret
down_revision: str | Sequence[str] | None = "b6e4a2c9d1f7"  # pragma: allowlist secret
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "oauth_clients",
        sa.Column("client_id", sa.String(), nullable=False),
        sa.Column("info", postgresql.JSONB(astext_type=sa.Text()), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.PrimaryKeyConstraint("client_id"),
    )
    op.add_column(
        "api_tokens",
        sa.Column(
            "kind",
            sa.Enum("STATIC", "OAUTH", name="apitokenkind", native_enum=False),
            nullable=False,
            server_default="STATIC",
        ),
    )
    op.alter_column("api_tokens", "kind", server_default=None)
    op.add_column(
        "api_tokens", sa.Column("oauth_client_id", sa.String(), nullable=True)
    )
    op.create_foreign_key(
        "fk_api_tokens_oauth_client_id",
        "api_tokens",
        "oauth_clients",
        ["oauth_client_id"],
        ["client_id"],
        ondelete="SET NULL",
    )
    op.create_table(
        "oauth_codes",
        sa.Column("code_hash", sa.String(), nullable=False),
        sa.Column("client_id", sa.String(), nullable=False),
        sa.Column("user_id", sa.Integer(), nullable=False),
        sa.Column("redirect_uri", sa.String(), nullable=False),
        sa.Column("redirect_uri_provided_explicitly", sa.Boolean(), nullable=False),
        sa.Column("code_challenge", sa.String(), nullable=False),
        sa.Column("resource", sa.String(), nullable=True),
        sa.Column(
            "access",
            sa.Enum("READ", "READ_WRITE", name="apitokenaccess", native_enum=False),
            nullable=False,
        ),
        sa.Column("all_cases", sa.Boolean(), nullable=False),
        sa.Column("case_ids", postgresql.JSONB(astext_type=sa.Text()), nullable=False),
        sa.Column("expires_at", sa.DateTime(timezone=True), nullable=False),
        sa.ForeignKeyConstraint(
            ["client_id"], ["oauth_clients.client_id"], ondelete="CASCADE"
        ),
        sa.ForeignKeyConstraint(["user_id"], ["users.id"], ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("code_hash"),
    )
    # The FK cascades (a purged client or user) and the pruning of expired codes scan these.
    op.create_index("ix_oauth_codes_client_id", "oauth_codes", ["client_id"])
    op.create_index("ix_oauth_codes_user_id", "oauth_codes", ["user_id"])
    op.create_table(
        "oauth_grants",
        sa.Column("api_token_id", sa.Integer(), nullable=False),
        sa.Column("refresh_hash", sa.String(), nullable=False),
        sa.Column("previous_refresh_hash", sa.String(), nullable=True),
        sa.Column("refresh_expires_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("access_expires_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("resource", sa.String(), nullable=True),
        sa.ForeignKeyConstraint(
            ["api_token_id"], ["api_tokens.id"], ondelete="CASCADE"
        ),
        sa.PrimaryKeyConstraint("api_token_id"),
        sa.UniqueConstraint("refresh_hash"),
    )
    # Reuse detection looks a presented refresh token up by its rotated-out hash.
    op.create_index(
        "ix_oauth_grants_previous_refresh_hash",
        "oauth_grants",
        ["previous_refresh_hash"],
    )


def downgrade() -> None:
    op.drop_table("oauth_grants")
    op.drop_table("oauth_codes")
    op.drop_constraint(
        "fk_api_tokens_oauth_client_id", "api_tokens", type_="foreignkey"
    )
    op.drop_column("api_tokens", "oauth_client_id")
    op.drop_column("api_tokens", "kind")
    op.drop_table("oauth_clients")
