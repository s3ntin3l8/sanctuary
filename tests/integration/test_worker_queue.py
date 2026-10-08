"""Integration tests for POST /api/v1/worker-queue/retry-failed.

Covers the retry_on_db_locked behaviour: one transient lock → retry succeeds;
permanent lock → skip-and-continue; dispatch_task called exactly once per
successfully-reset doc and never for skipped docs.
"""

from datetime import UTC, datetime
from unittest.mock import patch

import pytest
from sqlalchemy.exc import OperationalError

from app.models.database import Document, DocumentPipelineStage, IngestBatch
from app.models.enums import (
    IngestBatchSourceType,
    IngestBatchStatus,
    OriginatorType,
    PipelineStage,
    PipelineState,
    StageStatus,
)


def _make_failed_doc(db_session, sample_case) -> Document:
    batch = IngestBatch(
        source_type=IngestBatchSourceType.EMAIL,
        received_at=datetime.now(UTC),
        case_id=sample_case.id,
        status=IngestBatchStatus.FAILED,
    )
    db_session.add(batch)
    db_session.flush()

    doc = Document(
        title="Failed Doc",
        content="Some content",
        case_id=sample_case.id,
        originator_type=OriginatorType.COURT,
        ingest_batch_id=batch.id,
        pipeline_state=PipelineState.FAILED,
    )
    db_session.add(doc)
    db_session.flush()
    for s in PipelineStage:
        db_session.add(
            DocumentPipelineStage(
                document_id=doc.id,
                stage=s.value,
                status=StageStatus.FAILED.value,
            )
        )
    db_session.commit()
    return doc


@pytest.mark.integration
def test_retry_failed_succeeds_after_one_lock(app_client, db_session, sample_case):
    """A single transient db lock is retried; dispatch fires exactly once."""
    from app.services import pipeline_status as _pipeline_status_module

    _make_failed_doc(db_session, sample_case)

    call_count = 0
    real_reset = _pipeline_status_module.reset_failed_stages_only

    def flaky_reset(doc_id, db):
        nonlocal call_count
        call_count += 1
        if call_count == 1:
            raise OperationalError("database is locked", None, None)
        # Perform the real reset so the stage row is actually PENDING by the
        # time dispatch_pipeline_retry's claim_stage_for_dispatch runs —
        # a fake that only simulates success/failure without touching the
        # DB would make that claim correctly (and misleadingly) no-op.
        real_reset(doc_id, db)

    with (
        patch(
            "app.services.pipeline_status.reset_failed_stages_only",
            side_effect=flaky_reset,
        ),
        patch("app.tasks.dispatch.dispatch_task") as mock_dispatch,
    ):
        response = app_client.post("/api/v1/worker-queue/retry-failed")

    assert response.status_code == 200
    assert call_count == 2  # one retry consumed
    assert mock_dispatch.call_count == 1


@pytest.mark.integration
def test_retry_failed_skips_permanently_locked_doc(app_client, db_session, sample_case):
    """When all retries fail, the doc is skipped and the response is still 200."""
    _make_failed_doc(db_session, sample_case)

    def always_locked(doc_id, db):
        raise OperationalError("database is locked", None, None)

    with (
        patch(
            "app.services.pipeline_status.reset_failed_stages_only",
            side_effect=always_locked,
        ),
        patch("app.tasks.dispatch.dispatch_task") as mock_dispatch,
    ):
        response = app_client.post("/api/v1/worker-queue/retry-failed")

    assert response.status_code == 200
    mock_dispatch.assert_not_called()


@pytest.mark.integration
def test_retry_failed_dispatch_count_matches_reset_successes(
    app_client, db_session, sample_case
):
    """dispatch_task fires once per successfully-reset doc, never for skipped docs."""
    from app.services import pipeline_status as _pipeline_status_module

    doc1 = _make_failed_doc(db_session, sample_case)
    doc2 = _make_failed_doc(db_session, sample_case)

    always_fail_ids: set[int] = {doc2.id}
    real_reset = _pipeline_status_module.reset_failed_stages_only

    def selective_reset(doc_id, db):
        if doc_id in always_fail_ids:
            raise OperationalError("database is locked", None, None)
        # Perform the real reset -- see test_retry_failed_succeeds_after_one_lock.
        real_reset(doc_id, db)

    with (
        patch(
            "app.services.pipeline_status.reset_failed_stages_only",
            side_effect=selective_reset,
        ),
        patch("app.tasks.dispatch.dispatch_task") as mock_dispatch,
    ):
        response = app_client.post("/api/v1/worker-queue/retry-failed")

    assert response.status_code == 200
    assert mock_dispatch.call_count == 1
    dispatched_doc_id = mock_dispatch.call_args[0][1]
    assert dispatched_doc_id == doc1.id
    assert dispatched_doc_id != doc2.id


# --- Gmail import run surfaced on the queue ---------------------------------


def _import_run(uid: int, *, total=3, done=1, **extra):
    from app.services import gmail_runs

    state = {
        "run_id": "r1",
        "total": total,
        "done": done,
        "remaining": [],
        "failed": [],
        "sequential": True,
        "cancelled": False,
        "waiting_on": None,
        "current": {"subject": "Schriftsatz 8372/25"},
        "error": None,
        **extra,
    }
    assert gmail_runs.begin_run("import", uid, state)


@pytest.fixture
def gmail_runs_state(fake_run_state):
    return fake_run_state


def _admin_id(db_session) -> int:
    from app.models.database import User

    return db_session.query(User).filter_by(email="admin@localhost").one().id


def test_queue_has_no_gmail_import_when_none_ran(app_client, gmail_runs_state):
    body = app_client.get("/api/v1/worker-queue").json()
    assert body["gmail_import"] is None


def test_queue_shows_a_live_import_and_counts_what_is_left(
    app_client, db_session, gmail_runs_state
):
    _import_run(_admin_id(db_session), total=3, done=1)
    body = app_client.get("/api/v1/worker-queue").json()

    run = body["gmail_import"]
    assert run["active"] is True
    assert (run["done"], run["total"]) == (1, 3)
    assert run["current_subject"] == "Schriftsatz 8372/25"
    assert body["counts"]["queued"] == 2  # the two messages not yet fetched


def test_a_finished_import_stays_for_ten_minutes_then_goes(
    app_client, db_session, gmail_runs_state
):
    import json
    from datetime import timedelta

    from app.services import gmail_runs

    uid = _admin_id(db_session)
    _import_run(uid, total=3, done=3)
    gmail_runs.finish_run("import", uid, "r1")

    body = app_client.get("/api/v1/worker-queue").json()
    assert body["gmail_import"]["active"] is False
    assert body["counts"]["queued"] == 0  # finished: nothing awaiting

    key = f"sanctuary:gmail_import:{uid}"
    state = json.loads(gmail_runs_state.store[key])
    state["finished_at"] = (datetime.now(UTC) - timedelta(minutes=11)).isoformat()
    gmail_runs_state.store[key] = json.dumps(state)
    assert app_client.get("/api/v1/worker-queue").json()["gmail_import"] is None


def test_queue_survives_redis_being_down(app_client):
    import redis

    from app.services import gmail_runs

    class Down:
        def get(self, *_):
            raise redis.ConnectionError("down")

    with patch.object(gmail_runs, "_get_client", return_value=Down()):
        resp = app_client.get("/api/v1/worker-queue")
    assert resp.status_code == 200 and resp.json()["gmail_import"] is None
