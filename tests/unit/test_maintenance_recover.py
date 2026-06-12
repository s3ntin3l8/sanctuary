"""End-to-end test for the maintenance task that wires stale-job recovery.

`recover_pipeline_task` orchestrates four pipeline recovery functions plus
the two stale-job recovery helpers. The helpers themselves have unit tests
(`test_user_settings_service.py`), but no test exercises the orchestration
— a regression in the wire-up (missing call, missed db.commit) would let
stale jobs sit at status="running" forever.
"""

from datetime import timedelta

import pytest

from app.core.timezone import naive_utc_now
from app.models.database import AppSettings


def _stale_iso(minutes_ago: int) -> str:
    return (naive_utc_now() - timedelta(minutes=minutes_ago)).isoformat()


@pytest.fixture
def settings_row(db_session) -> AppSettings:
    # AppSettings is a singleton; the bootstrap-admin pin may already have created
    # the row, so get-or-create it rather than inserting a second one.
    row = db_session.query(AppSettings).first()
    if row is None:
        row = AppSettings(settings_json={})
        db_session.add(row)
    else:
        row.settings_json = {}
    db_session.commit()
    db_session.refresh(row)
    return row


@pytest.mark.unit
def test_recover_pipeline_task_flips_stale_jobs_end_to_end(
    db_session, settings_row, monkeypatch
):
    """Setup stale reindex + per-case dedup jobs alongside a fresh dedup job.
    Run the orchestrator. Only the stale jobs flip; the fresh one stays."""
    # Seed both job state machines.
    settings_row.settings_json = {
        "reindex_job": {
            "status": "running",
            "total": 100,
            "reindexed": 7,
            "failed": 0,
            "started_at": _stale_iso(90),
            "ended_at": None,
            "embed_dim": 768,
            "error": None,
        },
        "dedup_jobs": {
            "CASE-A": {
                "status": "running",
                "total": 30,
                "processed": 1,
                "started_at": _stale_iso(90),
                "ended_at": None,
            },
            "CASE-B": {
                "status": "running",
                "total": 20,
                "processed": 5,
                "started_at": _stale_iso(5),  # FRESH — must not flip
                "ended_at": None,
            },
        },
    }
    db_session.commit()

    # Isolate the stale-recovery branch by stubbing the four pipeline-recovery
    # functions. We're testing the orchestration, not the pipeline path.
    monkeypatch.setattr(
        "app.services.pipeline_status.recover_orphaned_running_stages",
        lambda db: {"docs_reset": 0, "stages_reset": 0},
    )
    monkeypatch.setattr(
        "app.services.pipeline_status.recover_stuck_pending_dispatches",
        lambda db: {"docs_redispatched": 0},
    )
    monkeypatch.setattr(
        "app.services.pipeline_status.recover_stuck_batches",
        lambda db: {"batches_recovered": 0},
    )
    monkeypatch.setattr(
        "app.services.pipeline_status.recover_unclaimed_ready_batches",
        lambda db: {"batches_dispatched": 0},
    )

    # Stub the SessionLocal the task uses so it talks to the test DB instead of
    # the production sqlite file. Task imports `from app.config import SessionLocal`
    # inside the function body, so patching app.config.SessionLocal is what reaches
    # the task at call-time.
    import app.config

    monkeypatch.setattr(app.config, "SessionLocal", lambda: db_session)

    from app.tasks.maintenance import recover_pipeline_task

    result = recover_pipeline_task()

    # Orchestration result reports both stale-job categories.
    assert result["status"] == "success"
    assert result["stale_reindex"] == 1
    assert result["stale_dedup"] == 1

    # The task's db.close() in `finally` evicts our settings_row from the
    # session's identity map; re-query rather than refresh.
    db_session.expire_all()
    row = db_session.query(AppSettings).first()
    job = row.settings_json["reindex_job"]
    assert job["status"] == "failed"
    assert "stale" in (job.get("error") or "")

    jobs = row.settings_json["dedup_jobs"]
    assert jobs["CASE-A"]["status"] == "failed"
    assert jobs["CASE-B"]["status"] == "running"  # untouched
