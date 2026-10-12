"""tag agent-written rows with the API token that wrote them

Revision ID: b6e4a2c9d1f7
Revises: a5d3f1b8c2e6
Create Date: 2026-10-12 14:00:00.000000

"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

# revision identifiers, used by Alembic.
revision: str = "b6e4a2c9d1f7"  # pragma: allowlist secret
down_revision: str | Sequence[str] | None = "a5d3f1b8c2e6"  # pragma: allowlist secret
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

_TABLES = (
    "user_reactions",
    "document_pins",
    "claims",
    "claim_evidence",
    "action_items",
    "document_relationships",
    "ingest_batches",
)


def upgrade() -> None:
    for table in _TABLES:
        op.add_column(table, sa.Column("agent_token_id", sa.Integer(), nullable=True))
        op.create_foreign_key(
            f"fk_{table}_agent_token_id",
            table,
            "api_tokens",
            ["agent_token_id"],
            ["id"],
            ondelete="SET NULL",
        )


def downgrade() -> None:
    for table in reversed(_TABLES):
        op.drop_constraint(f"fk_{table}_agent_token_id", table, type_="foreignkey")
        op.drop_column(table, "agent_token_id")
