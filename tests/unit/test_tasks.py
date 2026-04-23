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

    # Set pipeline_stages so enrichment shows as failed
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
    assert result["reason"] == "missing_enrichment"
    mock_mark_skipped.assert_called_once()
    mock_claims_delay.assert_called_once_with(sample_document.id)


@pytest.mark.unit
def test_extract_claims_failure_triggers_case_brief(db_session, sample_document):
    """When claim extraction fails permanently (retries exhausted), _trigger_case_brief is still called."""
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
