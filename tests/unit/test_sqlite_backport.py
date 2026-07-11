"""Regression tests for the pysqlite3 hermetic-SQLite fix (app/__init__.py).

sqlite-vec's KNN query requires SQLite >= 3.41 to work correctly; on an older
host libsqlite3 it silently returns wrong/empty results instead of failing
loudly (see app/__init__.py's docstring for the full incident). These tests
bind to the real engine object — the exact path the original bug lived
behind — rather than a fresh `import sqlite3`, so they track whether the fix
is actually wired up, not just installed.

A test that instead forced the historical `OperationalError` message would be
tautological: the fix supplies a newer SQLite, it adds no error-handling, so
forcing that message would exercise a code path the fix never touches and
would pass or fail independently of whether the fix is present.
"""

import pytest


@pytest.mark.unit
def test_engine_uses_pysqlite3_backport():
    """Catches the swap being removed or bypassed: SQLAlchemy's sqlite
    dialect must resolve to pysqlite3's bundled DBAPI, not the host's
    stdlib sqlite3. Identity check, not version — deterministic on any host,
    including CI's own already-modern stdlib."""
    import pysqlite3

    from app.config import engine

    assert engine.dialect.dbapi is pysqlite3.dbapi2


@pytest.mark.unit
def test_bundled_sqlite_meets_vec_floor():
    """Catches a future pysqlite3-binary release shipping a SQLite below the
    sqlite-vec KNN floor."""
    from app.config import engine

    assert engine.dialect.dbapi.sqlite_version_info >= (3, 41, 0)
