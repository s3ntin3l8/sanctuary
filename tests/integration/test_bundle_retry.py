"""Integration tests for POST /triage/bundle/retry."""

import json
from datetime import UTC, datetime
from unittest.mock import patch

import pytest

from app.models.database import Document, IngestBatch
from app.models.enums import (
    IngestBatchSourceType,
    IngestBatchStatus,
    OriginatorType,
    PipelineStage,
    PipelineState,
    StageStatus,
)


def _make_batch(
    db_session, sample_case, status=IngestBatchStatus.PENDING
) -> IngestBatch:
    batch = IngestBatch(
        source_type=IngestBatchSourceType.EMAIL,
        received_at=datetime.now(UTC),
        case_id=sample_case.id,
        status=status,
    )
    db_session.add(batch)
    db_session.flush()
    return batch


def _make_doc(
    db_session, batch: IngestBatch, pipeline_stages: dict | None = None
) -> Document:
    doc = Document(
        title="Test Doc",
        content="Some content",
        case_id=batch.case_id,
        originator_type=OriginatorType.COURT,
        ingest_batch_id=batch.id,
        pipeline_stages=pipeline_stages or {},
        pipeline_state=PipelineState.PENDING,
    )
    db_session.add(doc)
    db_session.flush()
    return doc


def _pending_stages() -> dict:
    return {s.value: {"status": StageStatus.PENDING.value} for s in PipelineStage}


def _stages_with_running(running_stage: PipelineStage) -> dict:
    stages = _pending_stages()
    stages[running_stage.value] = {"status": StageStatus.RUNNING.value}
    return stages


def _post(app_client, batch_id):
    return app_client.post("/triage/bundle/retry", data={"batch_id": batch_id})


@pytest.mark.integration
def test_retry_bundle_404_unknown_batch(app_client):
    response = _post(app_client, 99999)
    assert response.status_code == 404


@pytest.mark.integration
def test_retry_bundle_409_when_stage_running(app_client, db_session, sample_case):
    batch = _make_batch(db_session, sample_case)
    _make_doc(db_session, batch, _stages_with_running(PipelineStage.ENRICH))
    db_session.commit()

    response = _post(app_client, batch.id)
    assert response.status_code == 409


@pytest.mark.integration
def test_retry_bundle_happy_path(app_client, db_session, sample_case):
    batch = _make_batch(db_session, sample_case, status=IngestBatchStatus.PROCESSING)
    doc = _make_doc(db_session, batch, _pending_stages())
    db_session.commit()

    with patch("app.api.documents._dispatch_retry_task") as mock_dispatch:
        response = _post(app_client, batch.id)

    assert response.status_code == 200

    db_session.refresh(batch)
    db_session.refresh(doc)

    # Batch reset
    assert batch.analysis_queued_at is None
    assert batch.status == IngestBatchStatus.PENDING

    # All non-EXTRACT stages flipped to PENDING
    stages = doc.pipeline_stages or {}
    for stage in PipelineStage:
        if stage == PipelineStage.EXTRACT:
            continue
        assert stages[stage.value]["status"] == StageStatus.PENDING.value, (
            f"{stage.value} should be PENDING"
        )

    # Head-of-cascade dispatch: only METADATA (head) + EMBEDDINGS (parallel branch).
    # Downstream stages are handled by the natural cascade from each task.
    dispatched_stages = {call.args[2] for call in mock_dispatch.call_args_list}
    assert dispatched_stages == {PipelineStage.METADATA, PipelineStage.EMBEDDINGS}
    assert PipelineStage.EXTRACT not in dispatched_stages
    assert PipelineStage.ENRICH not in dispatched_stages

    # pipeline_state must be reset so the OOB row reflects the new status
    # (not the old FAILED/STUCK state). This is what makes the Retry button disappear.
    assert doc.pipeline_state != PipelineState.FAILED, (
        "pipeline_state still FAILED after retry — OOB row will render 'stuck' and "
        "the Retry button will remain visible."
    )

    # OOB row fragment must be in response body for client-side swap to work
    assert b'hx-swap-oob="true"' in response.content, (
        "OOB marker missing — render_bundle_group_oob silently failed or bundle was filtered out.\n"
        f"Response body: {response.content[:500]}"
    )
    assert f'id="triage-row-batch-{batch.id}"'.encode() in response.content, (
        f"Row ID missing — bundle lookup returned None.\nResponse body: {response.content[:500]}"
    )


@pytest.mark.integration
def test_retry_bundle_hx_trigger_payload(app_client, db_session, sample_case):
    batch = _make_batch(db_session, sample_case)
    _make_doc(db_session, batch, _pending_stages())
    db_session.commit()

    with patch("app.api.documents._dispatch_retry_task"):
        response = _post(app_client, batch.id)

    assert response.status_code == 200
    hx_trigger = response.headers.get("hx-trigger") or response.headers.get(
        "HX-Trigger"
    )
    assert hx_trigger, "HX-Trigger header must be present"
    payload = json.loads(hx_trigger)
    assert "triage:bundle-retried" in payload
    assert payload["triage:bundle-retried"]["batch_id"] == batch.id
    assert payload["triage:bundle-retried"]["doc_count"] == 1


@pytest.mark.integration
def test_retry_bundle_skips_skipped_stages(app_client, db_session, sample_case):
    """SKIPPED stages (e.g. BATCH_ANALYSIS on manual uploads) stay skipped."""
    stages = _pending_stages()
    stages[PipelineStage.BATCH_ANALYSIS.value] = {
        "status": StageStatus.SKIPPED.value,
        "reason": "no batch (manual upload)",
    }
    batch = _make_batch(db_session, sample_case)
    doc = _make_doc(db_session, batch, stages)
    db_session.commit()

    with patch("app.api.documents._dispatch_retry_task") as mock_dispatch:
        response = _post(app_client, batch.id)

    assert response.status_code == 200
    db_session.refresh(doc)
    stages_after = doc.pipeline_stages or {}
    assert (
        stages_after[PipelineStage.BATCH_ANALYSIS.value]["status"]
        == StageStatus.SKIPPED.value
    )

    dispatched_stages = {call.args[2] for call in mock_dispatch.call_args_list}
    assert PipelineStage.BATCH_ANALYSIS not in dispatched_stages


@pytest.mark.integration
def test_retry_bundle_preserves_extract_when_skipped_in_dispatch(
    app_client, db_session, sample_case
):
    """EXTRACT is never dispatched even if not SKIPPED."""
    stages = _pending_stages()
    stages[PipelineStage.EXTRACT.value] = {"status": StageStatus.COMPLETED.value}
    batch = _make_batch(db_session, sample_case)
    _make_doc(db_session, batch, stages)
    db_session.commit()

    with patch("app.api.documents._dispatch_retry_task") as mock_dispatch:
        response = _post(app_client, batch.id)

    assert response.status_code == 200
    dispatched_stages = {call.args[2] for call in mock_dispatch.call_args_list}
    assert PipelineStage.EXTRACT not in dispatched_stages
