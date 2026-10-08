"""Bounded orphan resets (#150), stranded EMBEDDINGS (#147), lost slicing prep (#166)."""

from datetime import timedelta
from unittest.mock import patch

import pytest
from sqlalchemy import text

from app.core.timezone import now_utc
from app.models.database import Document, IngestBatch
from app.models.enums import (
    IngestBatchSourceType,
    IngestBatchStatus,
    PipelineStage,
)
from app.services.pipeline_status import (
    initialize,
    mark_completed,
    recover_embeddings_behind_failed_stage,
    recover_orphaned_running_stages,
    recover_stuck_slicing_prep,
    reset_stage,
)


@pytest.fixture
def doc(db_session, sample_case):
    d = Document(title="D", content="x", case_id=sample_case.id)
    db_session.add(d)
    db_session.flush()
    initialize(d, batched=False, db=db_session)
    db_session.commit()
    return d


def _set(db, doc_id, stage, status, **cols):
    sets = ", ".join(["status = :s", *[f"{k} = :{k}" for k in cols]])
    db.execute(
        text(
            f"UPDATE document_pipeline_stages SET {sets} "
            "WHERE document_id = :d AND stage = :st"
        ),
        {"s": status, "d": doc_id, "st": stage, **cols},
    )
    db.execute(
        text("UPDATE documents SET pipeline_state = 'running' WHERE id = :d"),
        {"d": doc_id},
    )
    db.commit()


def _row(db, doc_id, stage):
    db.expire_all()
    return db.execute(
        text(
            "SELECT status, error, reason, orphan_resets FROM document_pipeline_stages "
            "WHERE document_id = :d AND stage = :s"
        ),
        {"d": doc_id, "s": stage},
    ).one()


def _hours_ago(h):
    return now_utc() - timedelta(hours=h)


# --- #150: poison-document cap -------------------------------------------------------


@pytest.mark.integration
def test_provable_orphan_is_reset_until_the_cap_then_failed_with_cascade(
    db_session, doc
):
    _set(db_session, doc.id, "extract", "completed")
    for expected in (1, 2, 3):
        _set(db_session, doc.id, "metadata", "running", started_at=_hours_ago(5))
        result = recover_orphaned_running_stages(db_session)
        assert result["stages_reset"] == 1 and result["stages_failed"] == 0
        status, _, _, resets = _row(db_session, doc.id, "metadata")
        assert (status, resets) == ("pending", expected)

    _set(db_session, doc.id, "metadata", "running", started_at=_hours_ago(5))
    result = recover_orphaned_running_stages(db_session)

    assert result["stages_failed"] == 1 and result["stages_reset"] == 0
    status, error, _, _ = _row(db_session, doc.id, "metadata")
    assert status == "failed" and "repeatedly" in error
    assert _row(db_session, doc.id, "enrich")[0] == "failed"  # cascaded
    # BATCH_ANALYSIS is batch-shared and deliberately not part of METADATA's cascade.
    assert _row(db_session, doc.id, "batch_analysis")[0] != "failed"


@pytest.mark.integration
def test_startup_style_reset_is_not_counted_against_the_document(db_session, doc):
    """A worker restart resets every RUNNING row; that says nothing about the doc."""
    _set(db_session, doc.id, "enrich", "running", started_at=now_utc())

    recover_orphaned_running_stages(db_session, min_age_seconds=0)

    status, _, _, resets = _row(db_session, doc.id, "enrich")
    assert (status, resets) == ("pending", 0)


@pytest.mark.integration
def test_completion_and_user_retry_clear_the_count(db_session, doc):
    _set(db_session, doc.id, "metadata", "running", started_at=_hours_ago(5))
    recover_orphaned_running_stages(db_session)
    assert _row(db_session, doc.id, "metadata")[3] == 1

    mark_completed(doc.id, PipelineStage.METADATA, db_session)
    assert _row(db_session, doc.id, "metadata")[3] == 0

    _set(db_session, doc.id, "enrich", "running", started_at=_hours_ago(5))
    recover_orphaned_running_stages(db_session)
    assert _row(db_session, doc.id, "enrich")[3] == 1
    assert reset_stage(doc.id, PipelineStage.ENRICH, db_session) is True
    assert _row(db_session, doc.id, "enrich")[3] == 0


# --- #147: EMBEDDINGS stranded behind a failed stage ------------------------------------


def _failed_doc(db, doc, *, extract, metadata, failed_ago_minutes=30):
    _set(db, doc.id, "extract", extract)
    _set(
        db,
        doc.id,
        "metadata",
        metadata,
        completed_at=now_utc() - timedelta(minutes=failed_ago_minutes),
    )
    db.execute(
        text("UPDATE documents SET pipeline_state = 'failed' WHERE id = :d"),
        {"d": doc.id},
    )
    db.commit()


@pytest.mark.integration
def test_pending_embeddings_behind_failed_metadata_are_dispatched_once(db_session, doc):
    _failed_doc(db_session, doc, extract="completed", metadata="failed")

    with patch("app.tasks.dispatch.dispatch_task") as dispatch:
        first = recover_embeddings_behind_failed_stage(db_session)
        second = recover_embeddings_behind_failed_stage(db_session)

    assert first["dispatched"] == 1 and first["doc_ids"] == [doc.id]
    assert dispatch.call_count == 1
    assert _row(db_session, doc.id, "embeddings")[0] == "running"  # claimed
    assert second["dispatched"] == 0  # no longer PENDING: the sweep cannot loop


@pytest.mark.integration
def test_pending_embeddings_are_skipped_when_extract_never_completed(db_session, doc):
    _failed_doc(db_session, doc, extract="failed", metadata="failed")

    with patch("app.tasks.dispatch.dispatch_task") as dispatch:
        result = recover_embeddings_behind_failed_stage(db_session)

    assert result["skipped"] == 1
    dispatch.assert_not_called()
    status, _, reason, _ = _row(db_session, doc.id, "embeddings")
    assert (status, reason) == ("skipped", "upstream_failed")


@pytest.mark.integration
def test_recent_failures_and_in_flight_metadata_are_left_alone(db_session, doc):
    _failed_doc(
        db_session, doc, extract="completed", metadata="failed", failed_ago_minutes=1
    )
    with patch("app.tasks.dispatch.dispatch_task") as dispatch:
        assert recover_embeddings_behind_failed_stage(db_session)["doc_ids"] == []
    dispatch.assert_not_called()

    # An older failure elsewhere, but METADATA still running: not stranded yet.
    _set(db_session, doc.id, "extract", "completed")
    _set(db_session, doc.id, "metadata", "running")
    _set(
        db_session,
        doc.id,
        "enrich",
        "failed",
        completed_at=now_utc() - timedelta(minutes=30),
    )
    db_session.execute(
        text("UPDATE documents SET pipeline_state = 'failed' WHERE id = :d"),
        {"d": doc.id},
    )
    db_session.commit()
    with patch("app.tasks.dispatch.dispatch_task") as dispatch:
        assert recover_embeddings_behind_failed_stage(db_session)["doc_ids"] == []
    dispatch.assert_not_called()
    assert _row(db_session, doc.id, "embeddings")[0] == "pending"


# --- #166: lost slicing preparation ------------------------------------------------------


def _slicing_batch(db, owner_id=None, **slicing):
    batch = IngestBatch(
        owner_id=owner_id,
        source_type=IngestBatchSourceType.MANUAL,
        status=IngestBatchStatus.AWAITING_SLICING,
        meta={"slicing": {"status": "preparing", **slicing}},
    )
    db.add(batch)
    db.commit()
    return batch


@pytest.mark.integration
def test_lost_slicing_prep_is_redispatched_once_then_failed_and_deletable(db_session):
    old = (now_utc() - timedelta(hours=5)).isoformat()
    batch = _slicing_batch(db_session, dispatched_at=old)

    with patch("app.tasks.dispatch.dispatch_task") as dispatch:
        first = recover_stuck_slicing_prep(db_session)
    db_session.refresh(batch)
    assert first["redispatched"] == [batch.id]
    assert dispatch.call_count == 1
    assert batch.meta["slicing"]["recovered"] is True
    assert batch.meta["slicing"]["status"] == "preparing"

    # Still preparing a full threshold later: give up so the user can retry/delete.
    meta = dict(batch.meta)
    meta["slicing"] = {**meta["slicing"], "dispatched_at": old}
    batch.meta = meta
    db_session.commit()
    with patch("app.tasks.dispatch.dispatch_task") as dispatch:
        second = recover_stuck_slicing_prep(db_session)
    db_session.refresh(batch)
    assert second["failed"] == [batch.id]
    dispatch.assert_not_called()
    assert batch.meta["slicing"]["status"] == "failed"

    from app.services.triage_dismissal import delete_bundle

    assert delete_bundle(db_session, batch.id) is not False  # no longer refused


@pytest.mark.integration
def test_fresh_and_legacy_slicing_prep_are_not_touched(db_session):
    fresh = _slicing_batch(db_session, dispatched_at=now_utc().isoformat())
    legacy = _slicing_batch(db_session)  # no timestamp: pre-dates this change

    with patch("app.tasks.dispatch.dispatch_task") as dispatch:
        result = recover_stuck_slicing_prep(db_session)

    dispatch.assert_not_called()
    assert result == {"redispatched": [], "failed": []}
    db_session.refresh(legacy)
    db_session.refresh(fresh)
    assert legacy.meta["slicing"]["status"] == "preparing"
    assert "dispatched_at" in legacy.meta["slicing"]  # its clock starts now


@pytest.mark.integration
def test_awaiting_slicing_batch_still_in_preparation_cannot_be_deleted(db_session):
    from app.services.triage_dismissal import delete_bundle

    batch = _slicing_batch(db_session, dispatched_at=now_utc().isoformat())

    with pytest.raises(ValueError):
        delete_bundle(db_session, batch.id)


@pytest.mark.integration
def test_prepare_records_failure_when_the_batch_has_no_source_file(db_session):
    from app.services.ingestion import slicer

    batch = _slicing_batch(db_session, dispatched_at=now_utc().isoformat())
    assert batch.raw_source_path is None

    with (
        patch("app.config.SessionLocal", lambda: db_session),
        patch.object(db_session, "close"),
        pytest.raises(ValueError),
    ):
        slicer.prepare(batch.id)

    db_session.refresh(batch)
    assert batch.meta["slicing"]["status"] == "failed"


_PASSWORD = "password123"  # pragma: allowlist secret


@pytest.mark.integration
def test_slicing_retry_stamps_a_fresh_dispatch_time_and_clears_recovered(
    auth_enabled, db_session
):
    from fastapi.testclient import TestClient

    from app.main import app
    from app.services import auth_service

    user = auth_service.create_user(
        db_session, email="slice@example.com", password=_PASSWORD
    )
    old = (now_utc() - timedelta(hours=9)).isoformat()
    batch = _slicing_batch(db_session, owner_id=user.id, dispatched_at=old)
    meta = dict(batch.meta)
    meta["slicing"] = {**meta["slicing"], "status": "failed", "recovered": True}
    batch.meta = meta
    db_session.commit()

    client = TestClient(app, follow_redirects=False)
    client.post(
        "/api/v1/auth/login", json={"email": "slice@example.com", "password": _PASSWORD}
    )
    with patch("app.tasks.dispatch.dispatch_task"):
        resp = client.post(f"/api/v1/slicing/{batch.id}/retry")

    assert resp.status_code == 200
    db_session.refresh(batch)
    slicing = batch.meta["slicing"]
    assert slicing["status"] == "preparing"
    assert slicing["dispatched_at"] > old
    assert "recovered" not in slicing
