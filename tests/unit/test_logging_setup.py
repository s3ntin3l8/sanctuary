"""Regression test for issue #98's root cause: alembic silently disabling loggers.

The app's own lifespan re-runs `alembic upgrade head` in-process on every
boot (app/main.py, right after startup). Alembic's env.py calls
`logging.config.fileConfig(alembic.ini)`, which defaults to
`disable_existing_loggers=True` -- silently setting `.disabled = True` on
every logger already registered at that point (app.main, app.access, ...)
that alembic.ini's own `[loggers]` section (root, sqlalchemy, alembic only)
doesn't list. `setup_logging()` is called again right after migrations to
recover from this, but it only reset level/handlers/propagate, never
`.disabled` -- so recovery never actually happened. Every log call through
those loggers (add_request_id, server_error_handler, AccessLogMiddleware)
was silently swallowed for the rest of the process's life, which is why
#98's confirm-route 500s produced zero log output despite three rounds of
added instrumentation (#99/#100/#102).

This test reproduces the real mechanism directly -- an actual alembic
`command.upgrade()` against a scratch on-disk sqlite db and a copy of the
real alembic.ini -- rather than mocking the disable, since the point is to
prove `setup_logging()` recovers from whatever alembic's own config does,
not to assert a hand-picked logging.config call.

A full app-boot end-to-end version of this (real lifespan, real
AccessLogMiddleware, real request) was verified manually in an isolated
subprocess rather than added here: reproducing it inside this shared test
process would require `importlib.reload(app.main)`, which mutates process
globals (the root logger's handlers, DATA_DIR, DATABASE_URL) that outlive
the test and are not undone by pytest's monkeypatch teardown -- a real risk
to every other test sharing this suite's process.
"""

import logging
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[2]


def test_setup_logging_recovers_loggers_disabled_by_alembic(tmp_path, monkeypatch):
    from alembic import command
    from alembic.config import Config as AlembicConfig

    # SANCTUARY_APP normally makes alembic/env.py skip fileConfig() entirely
    # -- unset it here to exercise the actual bug path (a bare `alembic`
    # invocation, or app/main.py's own in-process call before its
    # SANCTUARY_APP guard was added).
    monkeypatch.delenv("SANCTUARY_APP", raising=False)

    scratch_db = tmp_path / "regression.db"
    scratch_ini = tmp_path / "alembic.ini"
    ini_text = (REPO_ROOT / "alembic.ini").read_text()
    ini_text = ini_text.replace(
        "sqlalchemy.url = sqlite:///data/sanctuary.db",
        f"sqlalchemy.url = sqlite:///{scratch_db}",
    )
    ini_text = ini_text.replace(
        "script_location = %(here)s/alembic",
        f"script_location = {REPO_ROOT / 'alembic'}",
    )
    scratch_ini.write_text(ini_text)

    # A logger that stands in for app.main/app.access: it must already be
    # registered before alembic's fileConfig() runs, matching how app.main's
    # module-level `logger = logging.getLogger(__name__)` and
    # AccessLogMiddleware's `logging.getLogger("app.access")` are both
    # created well before the lifespan's migration call.
    marker = logging.getLogger("app._regression_marker_98")
    assert marker.disabled is False, "sanity: logger should start enabled"

    alembic_cfg = AlembicConfig(str(scratch_ini))
    command.upgrade(alembic_cfg, "head")

    assert marker.disabled is True, (
        "Expected alembic's own fileConfig() call to disable a pre-existing "
        "logger not listed in alembic.ini's [loggers] section. If this now "
        "fails, alembic.ini or alembic/env.py changed and this test's "
        "premise needs re-checking -- don't just delete the assertion."
    )

    from app.main import setup_logging

    setup_logging()

    assert marker.disabled is False, (
        "setup_logging() must reset `.disabled`, not just level/handlers/"
        "propagate -- otherwise any fileConfig()/dictConfig() call anywhere "
        "in the process (alembic's own in-process migration re-run being "
        "the concrete case from #98) permanently silences app logging for "
        "every logger that already existed at that point."
    )
