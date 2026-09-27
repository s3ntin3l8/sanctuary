"""Acceptance test for PR2 of the 2026-09-27 ingestion pipeline audit:
"one failed doc never blocks the case brief."

Before this PR, a batch with a single problem document could permanently
starve its case's brief:

- A METADATA failure used plain mark_failed (no cascade), so CLAIMS stayed
  PENDING forever for that doc — claim_case_brief_for_dispatch's readiness
  predicate never saw it as terminal.
- An ENRICH terminal failure either dispatched nothing downstream (the
  ReadTimeout/ConnectError/immediate-4xx branches), or dispatched
  RELATIONSHIPS/ENTITIES anyway (the other two branches) — which just hit
  their own ENRICH gate and re-skipped CLAIMS with a gate-block reason
  ("enrich_not_completed"), which the brief predicate treats as "still in
  flight," not terminal.
- RELATIONSHIPS errors were swallowed inside relationship_detector.detect()
  and returned as a skip reason string, so detect_relationships_task's own
  retry logic never ran.
- generate_case_brief_task's retries were dead too: case_brief_generator.
  generate() swallowed every exception internally.

This test drives a 4-doc batch — one healthy, one whose METADATA fails
(finishing last), one whose ENRICH exhausts retries, one whose RELATIONSHIPS
exhausts retries — through the real task functions (via .run(), since
conftest stubs every .delay()) and asserts the case brief still gets
dispatched, and that batch analysis actually ran once for the whole batch.
"""

from datetime import UTC, datetime
from unittest.mock import patch

import httpx
import pytest

from app.models.database import Case, Document, IngestBatch
from app.models.enums import (
    CaseStatus,
    IngestBatchSourceType,
    IngestBatchStatus,
    Jurisdiction,
    OriginatorType,
    PipelineStage,
)
from app.services.pipeline_status import initialize, mark_completed, stages_dict
from app.tasks.analyze_batch import analyze_batch_task
from app.tasks.detect_relationships import detect_relationships_task
from app.tasks.document_processing import metadata_task
from app.tasks.enrich_document import enrich_document_task
from app.tasks.extract_claims import extract_claims_task


def _make_doc(db_session, *, case_id, batch_id, title):
    doc = Document(
        title=title,
        content="content",
        case_id=case_id,
        ingest_batch_id=batch_id,
        originator_type=OriginatorType.COURT,
    )
    db_session.add(doc)
    db_session.flush()
    initialize(doc, batched=True, db=db_session)
    mark_completed(doc.id, PipelineStage.EXTRACT, db_session)
    db_session.commit()
    return doc


@pytest.mark.integration
def test_one_failed_doc_never_blocks_the_case_brief(db_session):
    case = Case(
        id="_PR2ACCEPT",
        title="PR2 acceptance",
        status=CaseStatus.INTAKE,
        jurisdiction=Jurisdiction.DE,
    )
    db_session.add(case)
    db_session.flush()

    batch = IngestBatch(
        source_type=IngestBatchSourceType.EMAIL,
        status=IngestBatchStatus.PROCESSING,
    )
    db_session.add(batch)
    db_session.flush()
    db_session.commit()

    doc_healthy = _make_doc(
        db_session, case_id=case.id, batch_id=batch.id, title="Healthy"
    )
    doc_rel_timeout = _make_doc(
        db_session, case_id=case.id, batch_id=batch.id, title="Relationships timeout"
    )
    doc_enrich_timeout = _make_doc(
        db_session, case_id=case.id, batch_id=batch.id, title="Enrich timeout"
    )
    doc_meta_fail = _make_doc(
        db_session, case_id=case.id, batch_id=batch.id, title="Metadata fails"
    )

    def _fake_summarize(doc_id, db):
        if doc_id == doc_meta_fail.id:
            raise ValueError("simulated non-transient metadata failure")
        # No-op success — METADATA's own output isn't exercised by this test.

    with (
        patch(
            "app.services.ai_summary._summarize_document_sync",
            side_effect=_fake_summarize,
        ),
        patch("app.tasks.analyze_batch.analyze_batch_task.delay") as mock_analyze_delay,
    ):
        # doc_meta_fail runs LAST — it's the straggler the batch claim is
        # waiting on when it fails.
        metadata_task(doc_healthy.id)
        metadata_task(doc_rel_timeout.id)
        metadata_task(doc_enrich_timeout.id)
        metadata_task(doc_meta_fail.id)

    # The batch claim must have fired despite the last doc's METADATA
    # failing — this is the "attempt the batch claim even on metadata_failed"
    # fix. Confirms the batch isn't stranded waiting for the 5-minute sweep.
    mock_analyze_delay.assert_called_once_with(batch.id)

    db_session.expire_all()
    meta_fail_stages = stages_dict(
        db_session.query(Document).filter(Document.id == doc_meta_fail.id).first()
    )
    assert meta_fail_stages["metadata"]["status"] == "failed"
    assert meta_fail_stages["enrich"]["status"] == "failed"
    assert meta_fail_stages["relationships"]["status"] == "failed"
    assert meta_fail_stages["claims"]["status"] == "failed"
    # BATCH_ANALYSIS must NOT have been cascade-failed by doc_meta_fail's
    # METADATA failure — that's the batch-shared-stage poisoning bug.
    assert meta_fail_stages["batch_analysis"]["status"] == "pending"

    with (
        patch(
            "app.services.intelligence.batch_analyzer.analyze", return_value=True
        ) as mock_analyze,
    ):
        analyze_batch_task.run(batch.id)

    mock_analyze.assert_called_once_with(batch.id)

    def _fake_enrich(doc_id):
        if doc_id == doc_enrich_timeout.id:
            raise httpx.ReadTimeout("simulated enrich timeout")
        doc = db_session.query(Document).filter(Document.id == doc_id).first()
        doc.ai_summary = {
            "legal_significance": "x",
            "action_deadline": "none",
            "financial_impact": "none",
        }
        doc.ai_summary_created_at = datetime.now(UTC)
        db_session.commit()

    def _fake_detect(doc_id):
        if doc_id == doc_rel_timeout.id:
            raise httpx.ReadTimeout("simulated relationships timeout")
        return None  # ran successfully, no relationships found

    with (
        patch(
            "app.services.intelligence.document_enricher.enrich",
            side_effect=_fake_enrich,
        ),
        patch(
            "app.services.intelligence.relationship_detector.detect",
            side_effect=_fake_detect,
        ),
        patch("app.services.intelligence.claim_extractor.extract", return_value=None),
        patch(
            "app.tasks.generate_case_brief.generate_case_brief_task.delay"
        ) as mock_brief_delay,
    ):
        # doc_healthy: full success chain.
        enrich_document_task.run(doc_healthy.id)
        detect_relationships_task.run(doc_healthy.id)
        extract_claims_task.run(doc_healthy.id)
        assert not mock_brief_delay.called  # 3 siblings still pending

        # doc_rel_timeout: ENRICH succeeds, RELATIONSHIPS exhausts retries.
        enrich_document_task.run(doc_rel_timeout.id)
        detect_relationships_task.request.update({"retries": 1})
        try:
            detect_relationships_task.run(doc_rel_timeout.id)
        finally:
            detect_relationships_task.request.clear()
        extract_claims_task.run(doc_rel_timeout.id)
        assert not mock_brief_delay.called  # doc_enrich_timeout still pending

        # doc_enrich_timeout: ENRICH exhausts retries — this is the last
        # doc to reach a CLAIMS-terminal state, via the cascade.
        enrich_document_task.request.update({"retries": 1})
        try:
            enrich_document_task.run(doc_enrich_timeout.id)
        finally:
            enrich_document_task.request.clear()

    # All four docs are now CLAIMS-terminal — the brief must have fired.
    mock_brief_delay.assert_called_once_with(case.id)

    db_session.expire_all()
    final_stages = {
        d.title: stages_dict(d)
        for d in db_session.query(Document).filter(Document.case_id == case.id).all()
    }
    assert final_stages["Healthy"]["claims"]["status"] == "completed"
    assert final_stages["Relationships timeout"]["relationships"]["status"] == "failed"
    assert final_stages["Relationships timeout"]["claims"]["status"] == "completed"
    assert final_stages["Enrich timeout"]["enrich"]["status"] == "failed"
    assert final_stages["Enrich timeout"]["claims"]["status"] == "failed"
    assert final_stages["Metadata fails"]["claims"]["status"] == "failed"
