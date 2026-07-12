"""Pin: FastAPI lifespan must skip production-DB side effects under pytest.

The lifespan calls `command.upgrade(alembic_cfg, "head")` against the dev
DATABASE_URL. When the dev server is running concurrently, this would race
its connections. The conftest already creates the test schema via
`Base.metadata.create_all(bind=test_engine)` on a separate test database, so
running migrations from the lifespan during tests is redundant *and*
dangerous.

Guard: skip migrations + seeding + recovery when `PYTEST_CURRENT_TEST` is set
(pytest sets this automatically per test).
"""

from unittest.mock import MagicMock, patch

import pytest


@pytest.mark.unit
def test_lifespan_skips_migrations_when_pytest_env_set(monkeypatch):
    """When PYTEST_CURRENT_TEST is set, lifespan must not invoke alembic."""
    import alembic.command as alembic_command

    from app.main import app, lifespan

    fake_upgrade = MagicMock()
    monkeypatch.setattr(alembic_command, "upgrade", fake_upgrade)
    monkeypatch.setenv("PYTEST_CURRENT_TEST", "tests/dummy.py::dummy")

    async def run():
        async with lifespan(app):
            pass

    import asyncio

    asyncio.run(run())

    assert not fake_upgrade.called, (
        "lifespan called alembic command.upgrade despite PYTEST_CURRENT_TEST "
        "being set — would race with the dev server's WAL locks."
    )


@pytest.mark.unit
def test_lifespan_runs_migrations_when_not_in_tests(monkeypatch):
    """Outside of pytest, lifespan must still migrate (production behavior)."""
    import alembic.command as alembic_command

    from app.main import app, lifespan

    fake_upgrade = MagicMock()
    monkeypatch.setattr(alembic_command, "upgrade", fake_upgrade)
    # Make sure the env var is unset for this test (we're inside pytest so
    # it's set on us, but the lifespan check should still fire).
    monkeypatch.delenv("PYTEST_CURRENT_TEST", raising=False)
    # Satisfy the no-auth/non-loopback security guard (the user's .env sets
    # HOST=0.0.0.0; production requires SESSION_SECRET to be set in that case).
    monkeypatch.setenv("SESSION_SECRET", "test-secret-for-lifespan-fixture")
    # And neutralise the other startup side-effects so we can isolate alembic.
    monkeypatch.setattr("app.services.case_service.seed_triage_case", lambda db: None)
    monkeypatch.setattr(
        "app.services.pipeline_status.recover_orphaned_running_stages",
        lambda db, **kwargs: {},
    )
    monkeypatch.setattr(
        "app.services.embeddings.verify_embedding_dim", lambda db, dim: (True, dim)
    )

    async def run():
        async with lifespan(app):
            pass

    import asyncio

    with patch("app.dependencies.SessionLocal"):
        asyncio.run(run())

    assert fake_upgrade.called, (
        "lifespan should run migrations in production mode (PYTEST_CURRENT_TEST unset)."
    )
