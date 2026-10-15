"""Case IDs can be renamed: every reference to cases.id cascades on update

Revision ID: a2c6e8f1b3d5
Revises: e1a7c3b5d9f4
Create Date: 2026-10-15 12:00:00.000000

"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

# revision identifiers, used by Alembic.
revision: str = "a2c6e8f1b3d5"  # pragma: allowlist secret
down_revision: str | Sequence[str] | None = "e1a7c3b5d9f4"  # pragma: allowlist secret
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def _recreate(on_update: str | None) -> None:
    insp = sa.inspect(op.get_bind())
    for table in insp.get_table_names():
        for fk in insp.get_foreign_keys(table):
            if fk["referred_table"] != "cases" or not fk["name"]:
                continue
            opts = fk.get("options", {})
            op.drop_constraint(fk["name"], table, type_="foreignkey")
            op.create_foreign_key(
                fk["name"],
                table,
                "cases",
                fk["constrained_columns"],
                fk["referred_columns"],
                ondelete=opts.get("ondelete"),
                onupdate=on_update,
            )


def upgrade() -> None:
    _recreate("CASCADE")


def downgrade() -> None:
    _recreate(None)
