"""entities.email: the mail address a correspondent writes from

Filled from the From header of a document whose sender matches the entity, so a
mail address maps to a contact. Nullable: most entities have none.

Revision ID: d4b8f2a6c1e9
Revises: c2e6a4b8d013
Create Date: 2026-10-17 18:00:00.000000

"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

# revision identifiers, used by Alembic.
revision: str = "d4b8f2a6c1e9"  # pragma: allowlist secret
down_revision: str | Sequence[str] | None = "c2e6a4b8d013"  # pragma: allowlist secret
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.add_column("entities", sa.Column("email", sa.String(), nullable=True))


def downgrade() -> None:
    op.drop_column("entities", "email")
