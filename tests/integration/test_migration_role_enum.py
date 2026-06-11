"""Migrations must seed/leave user roles in a form the ORM can load.

Regression test for the bootstrap-admin seed: the `phase1_user_accounts`
migration inserted the enum *value* (``'admin'``) with raw SQL, while the ORM
column ``role = Column(SAEnum(UserRole))`` round-trips the enum *name*
(``'ADMIN'``). Reading any user through the ORM then raised
``LookupError: 'admin' is not among the defined enum values``.

The rest of the suite builds its schema with ``Base.metadata.create_all`` and
seeds the admin through the ORM, so the migration SQL is never exercised there —
only a real ``alembic upgrade`` reproduces the bug.
"""

from pathlib import Path

import sqlalchemy as sa
from alembic import command
from alembic.config import Config
from sqlalchemy import create_engine
from sqlalchemy.orm import Session

from app.models.database import User
from app.models.enums import UserRole

REPO_ROOT = Path(__file__).resolve().parents[2]

# Revision immediately before the role-normalization migration.
REV_BEFORE_NORMALIZE = "c4e7a9b1d350"


def _alembic_config(db_path: Path) -> Config:
    """Alembic config for a throwaway sqlite DB.

    No ini file is passed (so env.py skips fileConfig); script_location and the
    DB URL are set directly. set_main_option populates the [alembic] section that
    env.py's engine_from_config reads.
    """
    cfg = Config()
    cfg.set_main_option("script_location", str(REPO_ROOT / "alembic"))
    cfg.set_main_option("sqlalchemy.url", f"sqlite:///{db_path}")
    return cfg


def _load_user_roles(db_path: Path) -> list[UserRole]:
    engine = create_engine(f"sqlite:///{db_path}")
    try:
        with Session(engine) as session:
            # The query resolves the role enum for every row; the seed bug
            # raised LookupError right here.
            return [u.role for u in session.query(User).all()]
    finally:
        engine.dispose()


def test_fresh_upgrade_seeds_orm_loadable_admin_role(tmp_path):
    """A clean `alembic upgrade head` seeds an admin the ORM can load."""
    db_path = tmp_path / "fresh.db"
    command.upgrade(_alembic_config(db_path), "head")

    roles = _load_user_roles(db_path)

    assert roles, "migrations should seed a bootstrap admin user"
    assert UserRole.ADMIN in roles, (
        f"bootstrap admin must persist as the UserRole.ADMIN name, got {roles!r}"
    )


def test_normalize_migration_repairs_legacy_lowercase_role(tmp_path):
    """A DB seeded under the old lowercase-value convention is reconciled.

    This is the dev-01 scenario: the admin row already exists as ``'admin'``.
    The normalization migration must rewrite it to the ``'ADMIN'`` name so the
    ORM stops raising LookupError.
    """
    db_path = tmp_path / "legacy.db"
    cfg = _alembic_config(db_path)

    # Stop before the normalization migration, then force the row to the legacy
    # lowercase value an old phase-1 seed would have written.
    command.upgrade(cfg, REV_BEFORE_NORMALIZE)
    engine = create_engine(f"sqlite:///{db_path}")
    with engine.begin() as conn:
        conn.execute(sa.text("UPDATE users SET role = 'admin'"))
    engine.dispose()

    command.upgrade(cfg, "head")

    roles = _load_user_roles(db_path)

    assert roles
    assert all(r is UserRole.ADMIN for r in roles), (
        f"legacy lowercase roles must be normalized to names, got {roles!r}"
    )
