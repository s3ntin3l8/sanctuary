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
