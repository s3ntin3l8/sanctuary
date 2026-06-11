"""normalize user role values to UserRole enum names

The ``users.role`` column is ``Column(SAEnum(UserRole))``, and SQLAlchemy's Enum
persists the enum *name* (``ADMIN`` / ``USER``). Earlier migrations seeded /
queried the lowercase enum *value* (``admin``), so any row written by those
migrations is rejected on ORM load with::

    LookupError: 'admin' is not among the defined enum values.

This reconciles existing rows to the name form. It is idempotent and a no-op on
databases that only ever wrote roles through the ORM.

Revision ID: 1193a719f300
Revises: c4e7a9b1d350
Create Date: 2026-06-11 22:12:57.283689

"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

# revision identifiers, used by Alembic.
revision: str = "1193a719f300"
down_revision: str | Sequence[str] | None = "c4e7a9b1d350"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    bind = op.get_bind()
    bind.execute(sa.text("UPDATE users SET role = 'ADMIN' WHERE role = 'admin'"))
    bind.execute(sa.text("UPDATE users SET role = 'USER' WHERE role = 'user'"))


def downgrade() -> None:
    bind = op.get_bind()
    bind.execute(sa.text("UPDATE users SET role = 'admin' WHERE role = 'ADMIN'"))
    bind.execute(sa.text("UPDATE users SET role = 'user' WHERE role = 'USER'"))
