from datetime import UTC
from unittest.mock import patch

import pytest

from app.tasks.detect_relationships import detect_relationships_task
from app.tasks.document_processing import (
    process_document_task,
    reingest_all_documents_task,
)
from app.tasks.enrich_document import enrich_document_task
from app.tasks.extract_claims import extract_claims_task


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
def test_detect_relationships_skips_and_dispatches_claims_when_enrichment_failed(
    db_session, sample_document
):
    """detect_relationships_task skips itself and dispatches extract_claims_task when enrichment is not completed."""
    from app.models.enums import PipelineStage, StageStatus

    sample_document.pipeline_stages = {
        PipelineStage.ENRICH.value: {"status": StageStatus.FAILED.value}
    }
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

    sample_document.pipeline_stages = {
        PipelineStage.ENRICH.value: {"status": StageStatus.COMPLETED.value}
    }
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

    sample_document.pipeline_stages = {
        PipelineStage.ENRICH.value: {"status": StageStatus.COMPLETED.value}
    }
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

    sample_document.pipeline_stages = {
        PipelineStage.ENRICH.value: {"status": StageStatus.COMPLETED.value}
    }
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

    sample_document.pipeline_stages = {
        PipelineStage.ENRICH.value: {"status": StageStatus.COMPLETED.value}
    }
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
