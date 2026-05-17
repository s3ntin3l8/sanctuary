"""Tests for the Processing Queue read endpoints' fail-fast busy_timeout.

The global `PRAGMA busy_timeout = 60000` (app/config.py) lets a contended
write path wait up to 60s for the writer lock. That's appropriate for worker
writes, but the /badge and /panel reads have a sub-second latency budget —
without an override, they hang for up to a minute when the cascade is busy,
which surfaces as "Loading…" forever in the UI. `_fail_fast_reads` overrides
the per-connection busy_timeout to 1000ms so a contended read raises within
~1s and the UI swaps the placeholder for an error state.
"""

import pytest
from sqlalchemy import text


@pytest.mark.unit
def test_fail_fast_reads_sets_one_second_busy_timeout(db_session):
    """_fail_fast_reads must override the connection's busy_timeout to ~1s.

    Reads the PRAGMA back on the same connection to prove the override took
    effect — relying on indirect proxies (e.g. timed contention) would make
    the test flaky on shared CI.
    """
    from app.api.worker_queue import _READ_BUSY_TIMEOUT_MS, _fail_fast_reads

    _fail_fast_reads(db_session)

    current = db_session.execute(text("PRAGMA busy_timeout")).scalar()
    assert current == _READ_BUSY_TIMEOUT_MS
    assert _READ_BUSY_TIMEOUT_MS == 1000  # contract: documented in the docstring


@pytest.mark.unit
def test_worker_queue_badge_endpoint_returns_200_when_quiet(app_client):
    """End-to-end: the /badge endpoint must serve normally when there's no
    contention. Regression guard against the PRAGMA override breaking the
    happy path."""
    response = app_client.get("/api/worker/queue/badge")
    assert response.status_code == 200


@pytest.mark.unit
def test_worker_queue_panel_endpoint_returns_200_when_quiet(app_client):
    """End-to-end: the /panel endpoint must serve normally when there's no
    contention."""
    response = app_client.get("/api/worker/queue/panel")
    assert response.status_code == 200
