from datetime import UTC
from unittest.mock import patch

import pytest
from sqlalchemy import text as _sa_text

from app.tasks.detect_relationships import detect_relationships_task
from app.tasks.document_processing import (
    process_document_task,
    reingest_all_documents_task,
)
from app.tasks.enrich_document import enrich_document_task
from app.tasks.extract_claims import extract_claims_task


def _set_doc_stages(db, doc, stages: dict) -> None:
    """Replace a doc's pipeline stages by upserting document_pipeline_stages rows."""
    db.execute(
        _sa_text("DELETE FROM document_pipeline_stages WHERE document_id = :id"),
        {"id": doc.id},
    )
    for stage_key, stage_data in stages.items():
        db.execute(
            _sa_text(
                "INSERT INTO document_pipeline_stages (document_id, stage, status) "
                "VALUES (:id, :stage, :status)"
            ),
            {
                "id": doc.id,
                "stage": stage_key,
                "status": stage_data.get("status", "pending"),
            },
        )
    db.expire(doc, ["stage_rows"])


@pytest.mark.unit
def test_claim_dedup_task_imports_and_is_registered():
    from app.tasks.celery_app import celery_app
    from app.tasks.claim_dedup import claim_dedup_task

    assert claim_dedup_task.name == "app.tasks.claim_dedup.claim_dedup_task"
    assert "app.tasks.claim_dedup" in celery_app.conf.include


@pytest.mark.unit
def test_process_document_task_success(db_session, sample_document):
    with (
        patch("app.tasks.document_processing.get_db_session") as mock_get_db_session,
        patch(
            "app.services.ingestion.service.process_uploaded_document"
        ) as mock_process_doc,
        patch("app.tasks.document_processing._run_phase1_summary"),
        patch("app.tasks.enrich_document.enrich_document_task.delay"),
        patch("app.tasks.generate_embedding.generate_embedding_task.delay"),
        patch.object(db_session, "close", return_value=None),
    ):
        mock_get_db_session.return_value = db_session

        result = process_document_task.run(sample_document.id)

        assert result["status"] == "success"
        mock_process_doc.assert_called_once()


@pytest.mark.unit
def test_reingest_all_documents_task(db_session, sample_document):
    with (
        patch("app.tasks.document_processing.get_db_session") as mock_get_db_session,
        patch(
            "app.tasks.document_processing.process_document_task.delay"
        ) as mock_delay,
        patch.object(db_session, "close", return_value=None),
    ):
        mock_get_db_session.return_value = db_session

        result = reingest_all_documents_task.run(case_id=sample_document.case_id)

        assert result["status"] == "queued"
        assert result["count"] == 1
        mock_delay.assert_called_once_with(sample_document.id)


# ---------------------------------------------------------------------------
# Pipeline resilience: always-dispatch chain on permanent failure
# ---------------------------------------------------------------------------


@pytest.mark.unit
def test_enrich_failure_dispatches_detect_relationships(db_session, sample_document):
    """When enrichment fails permanently (retries exhausted), detect_relationships_task is still dispatched."""
    # The doc must have batch_analysis terminal for ENRICH's primary gate to
    # let it through — see enrich_document.py's batch_analysis_not_completed
    # check. Production docs reach ENRICH via analyze_batch_task, which only
    # dispatches enrich once batch_analysis is done.
    _set_doc_stages(
        db_session, sample_document, {"batch_analysis": {"status": "completed"}}
    )
    with (
        patch("app.dependencies.get_db_session") as mock_get_db,
        patch(
            "app.services.intelligence.document_enricher.enrich",
            side_effect=RuntimeError("LLM timeout"),
        ),
        patch("app.services.pipeline_status.mark_started"),
        patch("app.services.pipeline_status.mark_failed"),
        patch(
            "app.tasks.detect_relationships.detect_relationships_task.delay"
        ) as mock_detect_delay,
        patch("app.tasks.enrich_document._trigger_cost_rollup"),
        patch.object(db_session, "close", return_value=None),
    ):
        mock_get_db.return_value = db_session
        enrich_document_task.request.update(
            {"retries": enrich_document_task.max_retries}
        )
        try:
            result = enrich_document_task.run(sample_document.id)
        finally:
            enrich_document_task.request.clear()

    assert result["status"] == "failed"
    mock_detect_delay.assert_called_once_with(sample_document.id)


@pytest.mark.unit
def test_enrich_skipped_when_batch_analysis_pending(db_session, sample_document):
    """Primary gate: ENRICH must defer (reset to PENDING, not SKIPPED) with
    reason=batch_analysis_not_completed when batch_analysis is not yet terminal.
    The old behavior was status='skipped', which broke _enrich_if_pending's CAS
    (requires status='pending') and silently stranded docs (ib-0033 root cause)."""
    _set_doc_stages(
        db_session, sample_document, {"batch_analysis": {"status": "pending"}}
    )
    with (
        patch("app.dependencies.get_db_session") as mock_get_db,
        patch("app.services.intelligence.document_enricher.enrich") as mock_enrich,
        patch.object(db_session, "close", return_value=None),
    ):
        mock_get_db.return_value = db_session
        result = enrich_document_task.run(sample_document.id)

    assert result["status"] == "deferred"
    assert result["reason"] == "batch_analysis_not_completed"
    # Critical: the actual AI call must NOT have happened.
    mock_enrich.assert_not_called()


@pytest.mark.unit
def test_enrich_retries_on_transient_4xx_from_lm_studio(db_session, sample_document):
    """A 4xx body containing an LM-Studio transient marker (Failed to load
    model / Model unloaded / etc.) must NOT immediate-fail like a genuine
    client error. Instead the task should call self.retry() and NOT mark the
    stage as failed on this attempt."""
    import httpx
    from celery.exceptions import Retry

    _set_doc_stages(
        db_session, sample_document, {"batch_analysis": {"status": "completed"}}
    )

    # Construct an HTTPStatusError whose str() carries the transient marker —
    # this is the shape _ai_call._stream_response now produces after the
    # body-capture change.
    request = httpx.Request("POST", "http://x/v1/chat/completions")
    response = httpx.Response(400, request=request)
    transient_err = httpx.HTTPStatusError(
        'HTTP 400 [400] BadRequestError: Lm_studioException - Failed to load model "qwen/qwen3.5-9b"',
        request=request,
        response=response,
    )

    # Stand-in Retry instance that the patched self.retry will raise.
    retry_sentinel = Retry(exc=transient_err)

    with (
        patch("app.dependencies.get_db_session") as mock_get_db,
        patch(
            "app.services.intelligence.document_enricher.enrich",
            side_effect=transient_err,
        ),
        patch("app.services.pipeline_status.mark_started"),
        patch("app.services.pipeline_status.mark_failed") as mock_mark_failed,
        patch("app.services.pipeline_status.schedule_retry") as mock_schedule_retry,
        patch.object(
            enrich_document_task, "retry", side_effect=retry_sentinel
        ) as mock_task_retry,
        patch("app.tasks.enrich_document._trigger_cost_rollup"),
        patch.object(db_session, "close", return_value=None),
    ):
        mock_get_db.return_value = db_session
        enrich_document_task.request.update({"retries": 0})
        try:
            with pytest.raises(Retry):
                enrich_document_task.run(sample_document.id)
        finally:
            enrich_document_task.request.clear()

    # The retry path was taken — schedule_retry recorded the attempt and
    # self.retry() was invoked.
    mock_schedule_retry.assert_called_once()
    mock_task_retry.assert_called_once()
    # Critical: must NOT have called mark_failed on the 4xx path.
    mock_mark_failed.assert_not_called()


@pytest.mark.unit
def test_enrich_immediate_fails_on_genuine_4xx(db_session, sample_document):
    """A 4xx body WITHOUT a transient marker (e.g. bare HTTP 400) must still
    immediate-fail — that's the existing client-side-error contract."""
    import httpx

    _set_doc_stages(
        db_session, sample_document, {"batch_analysis": {"status": "completed"}}
    )
    request = httpx.Request("POST", "http://x/v1/chat/completions")
    response = httpx.Response(400, request=request)
    client_err = httpx.HTTPStatusError(
        "HTTP 400 [400]", request=request, response=response
    )

    with (
        patch("app.dependencies.get_db_session") as mock_get_db,
        patch(
            "app.services.intelligence.document_enricher.enrich",
            side_effect=client_err,
        ),
        patch("app.services.pipeline_status.mark_started"),
        patch("app.services.pipeline_status.mark_failed") as mock_mark_failed,
        patch("app.tasks.enrich_document._trigger_cost_rollup"),
        patch.object(db_session, "close", return_value=None),
    ):
        mock_get_db.return_value = db_session
        enrich_document_task.request.update({"retries": 0})
        try:
            result = enrich_document_task.run(sample_document.id)
        finally:
            enrich_document_task.request.clear()

    assert result["status"] == "failed"
    mock_mark_failed.assert_called_once()


@pytest.mark.unit
def test_enrich_runs_when_batch_analysis_skipped_manual_upload(
    db_session, sample_document
):
    """Manual uploads have batch_analysis=SKIPPED (no batch). The gate must
    treat SKIPPED as terminal and let enrichment proceed."""
    _set_doc_stages(
        db_session,
        sample_document,
        {"batch_analysis": {"status": "skipped"}, "metadata": {"status": "completed"}},
    )
    with (
        patch("app.dependencies.get_db_session") as mock_get_db,
        patch("app.services.intelligence.document_enricher.enrich") as mock_enrich,
        patch("app.services.pipeline_status.mark_started"),
        patch("app.services.pipeline_status.mark_completed"),
        patch("app.tasks.enrich_document._dispatch_if_pending"),
        patch("app.tasks.enrich_document._trigger_cost_rollup"),
        patch.object(db_session, "close", return_value=None),
    ):
        mock_get_db.return_value = db_session
        result = enrich_document_task.run(sample_document.id)

    assert result["status"] == "success"
    mock_enrich.assert_called_once_with(sample_document.id)


@pytest.mark.unit
def test_detect_relationships_skips_and_dispatches_claims_when_enrichment_failed(
    db_session, sample_document
):
    """detect_relationships_task skips itself and dispatches extract_claims_task when enrichment is not completed."""
    from app.models.enums import PipelineStage, StageStatus

    _set_doc_stages(
        db_session,
        sample_document,
        {PipelineStage.ENRICH.value: {"status": StageStatus.FAILED.value}},
    )
    db_session.commit()

    with (
        patch("app.dependencies.get_db_session") as mock_get_db,
        patch("app.services.pipeline_status.mark_started"),
        patch("app.services.pipeline_status.mark_skipped") as mock_mark_skipped,
        patch(
            "app.tasks.extract_claims.extract_claims_task.delay"
        ) as mock_claims_delay,
        patch.object(db_session, "close", return_value=None),
    ):
        mock_get_db.return_value = db_session

        result = detect_relationships_task.run(sample_document.id)

    assert result["status"] == "skipped"
    assert result["reason"] == "enrich_not_completed"
    mock_mark_skipped.assert_called_once()
    mock_claims_delay.assert_called_once_with(sample_document.id)


@pytest.mark.unit
def test_detect_relationships_skips_and_dispatches_claims_when_ai_summary_missing(
    db_session, sample_document
):
    """detect_relationships_task skips when ENRICH completed but produced no ai_summary."""
    from app.models.enums import PipelineStage, StageStatus

    _set_doc_stages(
        db_session,
        sample_document,
        {PipelineStage.ENRICH.value: {"status": StageStatus.COMPLETED.value}},
    )
    # ai_summary_created_at is None — ENRICH ran but didn't produce a summary
    sample_document.ai_summary_created_at = None
    db_session.commit()

    with (
        patch("app.dependencies.get_db_session") as mock_get_db,
        patch("app.services.pipeline_status.mark_started"),
        patch("app.services.pipeline_status.mark_skipped") as mock_mark_skipped,
        patch(
            "app.tasks.extract_claims.extract_claims_task.delay"
        ) as mock_claims_delay,
        patch.object(db_session, "close", return_value=None),
    ):
        mock_get_db.return_value = db_session

        result = detect_relationships_task.run(sample_document.id)

    assert result["status"] == "skipped"
    assert result["reason"] == "missing_ai_summary"
    mock_mark_skipped.assert_called_once()
    mock_claims_delay.assert_called_once_with(sample_document.id)


@pytest.mark.unit
def test_detect_relationships_refreshes_review_reasons_on_success(
    db_session, sample_document
):
    """After relationships are detected, review_reasons is refreshed so the triage form
    shows unresolved_relationship before the user confirms."""
    from datetime import UTC, datetime

    from app.models.database import DocumentRelationship
    from app.models.enums import (
        PipelineStage,
        RelationshipConfidence,
        RelationshipType,
        StageStatus,
    )

    _set_doc_stages(
        db_session,
        sample_document,
        {PipelineStage.ENRICH.value: {"status": StageStatus.COMPLETED.value}},
    )
    sample_document.ai_summary_created_at = datetime.now(UTC)
    sample_document.review_reasons = []
    sample_document.needs_review = False
    db_session.commit()

    # Create a second doc so the relationship FK is valid
    from app.models.database import Document
    from app.models.enums import OriginatorType

    other_doc = Document(
        title="Other Doc",
        content="...",
        case_id=sample_document.case_id,
        originator_type=OriginatorType.COURT,
    )
    db_session.add(other_doc)
    db_session.commit()
    db_session.refresh(other_doc)

    # Simulate what detect() produces: an AI_DETECTED relationship edge
    rel = DocumentRelationship(
        from_document_id=sample_document.id,
        to_document_id=other_doc.id,
        relationship_type=RelationshipType.REFERENCES,
        confidence=RelationshipConfidence.AI_DETECTED,
    )
    db_session.add(rel)
    db_session.commit()

    with (
        patch("app.dependencies.get_db_session") as mock_get_db,
        patch("app.services.pipeline_status.mark_started"),
        patch("app.services.pipeline_status.mark_completed"),
        patch(
            "app.services.intelligence.relationship_detector.detect", return_value=None
        ),
        patch("app.tasks.extract_claims.extract_claims_task.delay"),
        patch.object(db_session, "close", return_value=None),
    ):
        mock_get_db.return_value = db_session

        result = detect_relationships_task.run(sample_document.id)

    assert result["status"] == "success"
    db_session.refresh(sample_document)
    assert "unresolved_relationship" in (sample_document.review_reasons or [])
    assert sample_document.needs_review is True


@pytest.mark.unit
def test_extract_claims_refreshes_review_reasons_on_success(
    db_session, sample_document
):
    """After claims are extracted with adversarial evidence, review_reasons is refreshed
    so the triage card shows contests_existing_claim before the user confirms."""
    from datetime import UTC, datetime

    from app.models.database import Claim, ClaimEvidence
    from app.models.enums import (
        ClaimEvidenceRole,
        ClaimStatus,
        ClaimType,
        PipelineStage,
        RelationshipConfidence,
        StageStatus,
    )

    _set_doc_stages(
        db_session,
        sample_document,
        {PipelineStage.ENRICH.value: {"status": StageStatus.COMPLETED.value}},
    )
    sample_document.ai_summary_created_at = datetime.now(UTC)
    sample_document.review_reasons = []
    sample_document.needs_review = False
    db_session.commit()

    # A pre-existing claim in the case (Wave 2A: claim is global, scope
    # comes from its evidence-document's case).
    claim = Claim(
        claim_text="Opposing party claim",
        claim_type=ClaimType.FACTUAL,
        status=ClaimStatus.ASSERTED,
    )
    db_session.add(claim)
    db_session.flush()
    db_session.add_all(
        [
            ClaimEvidence(
                claim_id=claim.id,
                document_id=sample_document.id,
                role=ClaimEvidenceRole.ASSERTS,
                confidence=RelationshipConfidence.AI_DETECTED,
            ),
            ClaimEvidence(
                claim_id=claim.id,
                document_id=sample_document.id,
                role=ClaimEvidenceRole.CONTESTS,
                confidence=RelationshipConfidence.AI_DETECTED,
            ),
        ]
    )
    db_session.commit()
    db_session.refresh(claim)

    with (
        patch("app.dependencies.get_db_session") as mock_get_db,
        patch("app.services.pipeline_status.mark_started"),
        patch("app.services.pipeline_status.mark_completed"),
        patch("app.services.intelligence.claim_extractor.extract", return_value=None),
        patch("app.tasks.extract_claims._trigger_case_brief"),
        patch.object(db_session, "close", return_value=None),
    ):
        mock_get_db.return_value = db_session

        from app.tasks.extract_claims import extract_claims_task

        result = extract_claims_task.run(sample_document.id)

    assert result["status"] == "success"
    db_session.refresh(sample_document)
    assert "contests_existing_claim" in (sample_document.review_reasons or [])
    assert sample_document.needs_review is True


@pytest.mark.unit
def test_extract_claims_failure_triggers_case_brief(db_session, sample_document):
    """When claim extraction fails permanently (retries exhausted), _trigger_case_brief is still called."""
    from datetime import datetime

    from app.models.enums import PipelineStage, StageStatus

    _set_doc_stages(
        db_session,
        sample_document,
        {PipelineStage.ENRICH.value: {"status": StageStatus.COMPLETED.value}},
    )
    sample_document.ai_summary_created_at = datetime.now(UTC)
    db_session.commit()

    with (
        patch("app.dependencies.get_db_session") as mock_get_db,
        patch(
            "app.services.intelligence.claim_extractor.extract",
            side_effect=RuntimeError("LLM timeout"),
        ),
        patch("app.services.pipeline_status.mark_started"),
        patch("app.services.pipeline_status.mark_failed"),
        patch("app.tasks.extract_claims._trigger_case_brief") as mock_trigger_brief,
        patch.object(db_session, "close", return_value=None),
    ):
        mock_get_db.return_value = db_session
        extract_claims_task.request.update({"retries": extract_claims_task.max_retries})
        try:
            result = extract_claims_task.run(sample_document.id)
        finally:
            extract_claims_task.request.clear()

    assert result["status"] == "failed"
    mock_trigger_brief.assert_called_once_with(sample_document.id)
