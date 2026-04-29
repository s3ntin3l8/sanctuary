"""Tests for SQLite PRAGMA configuration in app.config.load_sqlite_extensions."""

from sqlalchemy import create_engine, event, text


def test_load_sqlite_extensions_enables_foreign_keys():
    from app.config import load_sqlite_extensions

    test_engine = create_engine(
        "sqlite:///:memory:", connect_args={"check_same_thread": False}
    )
    event.listen(test_engine, "connect", load_sqlite_extensions)

    with test_engine.connect() as conn:
        result = conn.execute(text("PRAGMA foreign_keys")).scalar()

    assert result == 1, (
        "load_sqlite_extensions does not enable PRAGMA foreign_keys=ON — "
        "every FK in the schema is silently unenforced at runtime"
    )
