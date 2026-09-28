"""Unit tests for app/services/triage_retry.py's retry-dispatch primitives.

PR4 (2026-09-27 ingestion audit, F4): dispatch_pipeline_retry previously
bypassed claim_stage_for_dispatch entirely, and reset_batch_for_retry treated
only RUNNING (not RETRYING) as in-flight and never cleared a batch's
metadata_phase_queued_at on a full retry.
"""

import pytest
from sqlalchemy import text as _sql_text

from app.models.database import Case, Document, IngestBatch
from app.models.enums import (
    CaseStatus,
    IngestBatchSourceType,
    IngestBatchStatus,
    Jurisdiction,
    OriginatorType,
    PipelineStage,
    StageStatus,
)
from app.services.pipeline_status import (
    claim_stage_for_dispatch,
    initialize,
    mark_completed,
    stages_dict,
)
from app.services.triage_retry import dispatch_pipeline_retry, reset_batch_for_retry
from app.tasks.generate_embedding import generate_embedding_task


def _make_doc(db_session, *, case_id="_TRIAGE", batch_id=None):
    doc = Document(
        title="x",
        content="x",
        case_id=case_id,
        ingest_batch_id=batch_id,
        originator_type=OriginatorType.UNKNOWN,
    )
    db_session.add(doc)
    db_session.flush()
    initialize(doc, batched=batch_id is not None, db=db_session)
    db_session.commit()
    return doc


def _set_stage(db_session, doc, stage: PipelineStage, status: StageStatus) -> None:
    db_session.execute(
        _sql_text(
            "UPDATE document_pipeline_stages SET status = :status "
            "WHERE document_id = :doc_id AND stage = :stage"
        ),
        {"status": status.value, "doc_id": doc.id, "stage": stage.value},
    )
    db_session.expire(doc, ["stage_rows"])
    db_session.commit()


@pytest.mark.unit
def test_dispatch_pipeline_retry_metadata_dispatches_unclaimed(
    db_session, mock_dispatch_task
):
    """METADATA is self_claims=True: dispatch_pipeline_retry must NOT
    pre-claim it (flip PENDING->RUNNING itself) — metadata_task claims its
    own stage on entry, and a pre-claim here would leave it RUNNING with the
    dispatched task then seeing RUNNING and skipping as "already_claimed" —
    a permanent deadlock."""
    doc = _make_doc(db_session)

    dispatch_pipeline_retry(doc.id, None, PipelineStage.METADATA, db_session)

    mock_dispatch_task.assert_called_once_with(
        "app.tasks.document_processing.metadata_task", doc.id
    )
    db_session.expire(doc, ["stage_rows"])
    assert stages_dict(doc)["metadata"]["status"] == StageStatus.PENDING.value


@pytest.mark.unit
def test_dispatch_pipeline_retry_extract_claims_before_dispatch(
    db_session, mock_dispatch_task
):
    """EXTRACT is not self-claiming — mark_started runs unconditionally, so
    dispatch_pipeline_retry must pre-claim it (flip PENDING->RUNNING) before
    dispatching, for the same dedup every other cascade dispatcher relies
    on."""
    doc = _make_doc(db_session)

    dispatch_pipeline_retry(doc.id, None, PipelineStage.EXTRACT, db_session)

    mock_dispatch_task.assert_called_once_with(
        "app.tasks.document_processing.process_document_task", doc.id
    )
    db_session.expire(doc, ["stage_rows"])
    assert stages_dict(doc)["extract"]["status"] == StageStatus.RUNNING.value


@pytest.mark.unit
def test_dispatch_pipeline_retry_extract_skips_when_already_claimed(
    db_session, mock_dispatch_task
):
    """If EXTRACT is already RUNNING (claimed by someone else), the claim
    must fail and no second dispatch should fire."""
    doc = _make_doc(db_session)
    _set_stage(db_session, doc, PipelineStage.EXTRACT, StageStatus.RUNNING)

    dispatch_pipeline_retry(doc.id, None, PipelineStage.EXTRACT, db_session)

    mock_dispatch_task.assert_not_called()


@pytest.mark.unit
def test_dispatch_pipeline_retry_embeddings_dispatches_unclaimed(
    db_session, mock_dispatch_task
):
    """EMBEDDINGS is self_claims=True: generate_embedding_task defers
    unclaimed (leaves the stage PENDING) when METADATA isn't yet terminal,
    so metadata_task's own cascade can re-claim it later. Pre-claiming here
    would leave the stage RUNNING on defer instead of PENDING, permanently
    breaking that later re-claim and silently losing the embedding forever —
    reproduces a regression caught in PR4 review: dispatch_batch_retry fires
    EMBEDDINGS in parallel with a head-stage retry that can land well before
    METADATA is terminal again."""
    doc = _make_doc(db_session)  # METADATA starts pending

    dispatch_pipeline_retry(doc.id, None, PipelineStage.EMBEDDINGS, db_session)

    mock_dispatch_task.assert_called_once_with(
        "app.tasks.generate_embedding.generate_embedding_task", doc.id
    )
    db_session.expire(doc, ["stage_rows"])
    assert stages_dict(doc)["embeddings"]["status"] == StageStatus.PENDING.value


@pytest.mark.unit
def test_embeddings_deferral_leaves_stage_reclaimable_after_metadata_completes(
    db_session,
):
    """End-to-end reproduction of the EMBEDDINGS-stranding regression: after
    dispatch_pipeline_retry hands EMBEDDINGS off unclaimed, the real deferred
    task run must leave the stage PENDING (not RUNNING), so that
    metadata_task's own claim_stage_for_dispatch cascade can successfully
    re-claim and redispatch it once METADATA actually completes."""
    doc = _make_doc(db_session)  # METADATA starts pending

    # The task itself, run directly (dispatch_task/.delay is not the thing
    # under test here) -- simulates the dispatched task actually executing
    # while METADATA is still pending.
    result = generate_embedding_task.run(doc.id)

    assert result["status"] == "deferred"
    db_session.expire(doc, ["stage_rows"])
    assert stages_dict(doc)["embeddings"]["status"] == StageStatus.PENDING.value

    # Now METADATA completes -- metadata_task's cascade must be able to
    # claim EMBEDDINGS for real.
    mark_completed(doc.id, PipelineStage.METADATA, db_session)
    assert claim_stage_for_dispatch(doc.id, PipelineStage.EMBEDDINGS, db_session)


@pytest.mark.unit
def test_dispatch_pipeline_retry_batch_analysis_dedup(db_session, mock_dispatch_task):
    """BATCH_ANALYSIS is batch-shared: two docs in the same batch that both
    compute it as their head stage must only trigger analyze_batch_task
    once, via the batch-level claim_batch_for_analysis CAS."""
    case = Case(
        id="_TR_TRETRY1",
        title="T",
        status=CaseStatus.INTAKE,
        jurisdiction=Jurisdiction.DE,
    )
    db_session.add(case)
    db_session.commit()

    batch = IngestBatch(source_type=IngestBatchSourceType.EMAIL)
    db_session.add(batch)
    db_session.flush()

    doc1 = _make_doc(db_session, case_id=case.id, batch_id=batch.id)
    doc2 = _make_doc(db_session, case_id=case.id, batch_id=batch.id)
    for doc in (doc1, doc2):
        mark_completed(doc.id, PipelineStage.EXTRACT, db_session)
        mark_completed(doc.id, PipelineStage.METADATA, db_session)
    db_session.commit()

    dispatch_pipeline_retry(doc1.id, batch.id, PipelineStage.BATCH_ANALYSIS, db_session)
    dispatch_pipeline_retry(doc2.id, batch.id, PipelineStage.BATCH_ANALYSIS, db_session)

    mock_dispatch_task.assert_called_once_with(
        "app.tasks.analyze_batch.analyze_batch_task", batch.id
    )


@pytest.mark.unit
def test_reset_batch_for_retry_blocks_on_retrying_stage(db_session):
    """A RETRYING stage still has a live scheduled countdown — reset_batch_
    for_retry must treat it the same as RUNNING and refuse to reset."""
    case = Case(
        id="_TR_TRETRY2",
        title="T",
        status=CaseStatus.INTAKE,
        jurisdiction=Jurisdiction.DE,
    )
    db_session.add(case)
    db_session.commit()

    batch = IngestBatch(source_type=IngestBatchSourceType.EMAIL)
    db_session.add(batch)
    db_session.flush()
    doc = _make_doc(db_session, case_id=case.id, batch_id=batch.id)
    _set_stage(db_session, doc, PipelineStage.EXTRACT, StageStatus.RETRYING)
    db_session.refresh(batch)

    result = reset_batch_for_retry(batch, db_session, full=False)

    assert result == -1


@pytest.mark.unit
def test_reset_batch_for_retry_clears_metadata_phase_queued_at_on_full(db_session):
    """metadata_phase_queued_at is a one-time CAS flag that never resets on
    its own. A full retry resets EXTRACT back to PENDING for every doc, so
    it must also clear this flag or the batch's METADATA phase can never
    fire again once every doc completes EXTRACT a second time."""
    from datetime import UTC, datetime

    case = Case(
        id="_TR_TRETRY3",
        title="T",
        status=CaseStatus.INTAKE,
        jurisdiction=Jurisdiction.DE,
    )
    db_session.add(case)
    db_session.commit()

    batch = IngestBatch(
        source_type=IngestBatchSourceType.EMAIL,
        status=IngestBatchStatus.PROCESSING,
    )
    batch.metadata_phase_queued_at = datetime.now(UTC).replace(tzinfo=None)
    db_session.add(batch)
    db_session.flush()
    _make_doc(db_session, case_id=case.id, batch_id=batch.id)
    db_session.commit()
    db_session.refresh(batch)

    result = reset_batch_for_retry(batch, db_session, full=True)

    assert result != -1
    assert batch.metadata_phase_queued_at is None


@pytest.mark.unit
def test_reset_batch_for_retry_partial_does_not_clear_metadata_phase_queued_at(
    db_session,
):
    """A partial (non-full) retry leaves EXTRACT completed, so the metadata
    phase already correctly fired once for this batch and must not be
    cleared — clearing it here would let a second, redundant metadata-phase
    dispatch fire once EXTRACT (untouched) is re-read as still completed."""
    from datetime import UTC, datetime

    case = Case(
        id="_TR_TRETRY4",
        title="T",
        status=CaseStatus.INTAKE,
        jurisdiction=Jurisdiction.DE,
    )
    db_session.add(case)
    db_session.commit()

    batch = IngestBatch(
        source_type=IngestBatchSourceType.EMAIL,
        status=IngestBatchStatus.PROCESSING,
    )
    queued_at = datetime.now(UTC).replace(tzinfo=None)
    batch.metadata_phase_queued_at = queued_at
    db_session.add(batch)
    db_session.flush()
    doc = _make_doc(db_session, case_id=case.id, batch_id=batch.id)
    mark_completed(doc.id, PipelineStage.EXTRACT, db_session)
    db_session.commit()
    db_session.refresh(batch)

    result = reset_batch_for_retry(batch, db_session, full=False)

    assert result != -1
    assert batch.metadata_phase_queued_at is not None
    assert batch.metadata_phase_queued_at.replace(tzinfo=None) == queued_at
