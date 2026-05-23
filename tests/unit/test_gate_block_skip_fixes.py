"""Tests for the gate-block-skip race fixes (ib-0033 stranding).

Root cause: recover_stuck_pending_dispatches was treating pending BATCH_ANALYSIS
as invisible (unconditional `continue`), so the recovery cron dispatched
enrich_document_task prematurely. The enrich gate then marked ENRICH=SKIPPED,
and _enrich_if_pending's CAS could not reclaim SKIPPED rows — the doc was
silently stranded for the rest of the pipeline.

Covers fixes A–E from the plan:
  A: recover_stuck_pending_dispatches blocks on pending BATCH_ANALYSIS
  B: enrich gate-block resets to PENDING not SKIPPED
  C: recover_stuck_batches guards against already-completed batches
  D: claim_batch_for_analysis refuses when batch_analysis already terminal
  E: recover_stranded_gate_skipped finds and unstrands affected docs
"""

from datetime import UTC, datetime, timedelta

import pytest
from sqlalchemy import text

from app.models.database import Document, IngestBatch
from app.models.enums import (
    IngestBatchSourceType,
    IngestBatchStatus,
    OriginatorType,
    PipelineState,
)
from app.services.pipeline_status import (
    initialize,
    recover_stranded_gate_skipped,
    recover_stuck_batches,
    recover_stuck_pending_dispatches,
)


def _upsert_stage(db, doc_id: int, stage: str, status: str, reason: str | None = None):
    db.execute(
        text(
            """
            INSERT INTO document_pipeline_stages (document_id, stage, status, reason)
            VALUES (:doc_id, :stage, :status, :reason)
            ON CONFLICT(document_id, stage) DO UPDATE
              SET status=:status, reason=:reason
            """
        ),
        {"doc_id": doc_id, "stage": stage, "status": status, "reason": reason},
    )
    db.commit()


def _make_doc_with_batch(db, case_id: str, batch_id: int) -> Document:
    doc = Document(
        title="Test",
        content="x",
        case_id=case_id,
        ingest_batch_id=batch_id,
        originator_type=OriginatorType.COURT,
        pipeline_state=PipelineState.PARTIAL,
        ingest_date=datetime.now(UTC) - timedelta(minutes=5),
    )
    db.add(doc)
    db.flush()
    initialize(doc, batched=True, db=db)
    db.commit()
    db.refresh(doc)
    return doc


def _make_batch(db, case_id: str, *, analysis_queued_at=None) -> IngestBatch:
    batch = IngestBatch(
        source_type=IngestBatchSourceType.EMAIL,
        case_id=case_id,
        status=IngestBatchStatus.PROCESSING,
        received_at=datetime.now(),
        ingest_date=datetime.now(),
        analysis_queued_at=analysis_queued_at,
    )
    db.add(batch)
    db.flush()
    return batch


# ---------------------------------------------------------------------------
# Fix A: recover_stuck_pending_dispatches must not dispatch past pending BATCH_ANALYSIS
# ---------------------------------------------------------------------------


@pytest.mark.unit
def test_recover_does_not_dispatch_enrich_when_batch_analysis_pending(
    db_session, sample_case, monkeypatch
):
    """When BATCH_ANALYSIS is PENDING, the recovery sweep must NOT dispatch ENRICH.
    Previously, the unconditional `continue` for skip_stages made BATCH_ANALYSIS
    invisible and ENRICH would be dispatched prematurely — triggering the
    gate-block-skip race that stranded ib-0033."""
    batch = _make_batch(db_session, sample_case.id)
    doc = _make_doc_with_batch(db_session, sample_case.id, batch.id)

    # Simulate: EXTRACT + METADATA + EMBEDDINGS completed, BATCH_ANALYSIS still PENDING.
    # EMBEDDINGS is order 2 (before BATCH_ANALYSIS order 3), so it must be terminal
    # here — otherwise the recovery would dispatch EMBEDDINGS instead of hitting
    # the BATCH_ANALYSIS blocker we're testing.
    _upsert_stage(db_session, doc.id, "extract", "completed")
    _upsert_stage(db_session, doc.id, "metadata", "completed")
    _upsert_stage(db_session, doc.id, "embeddings", "completed")
    _upsert_stage(db_session, doc.id, "batch_analysis", "pending")
    _upsert_stage(db_session, doc.id, "enrich", "pending")

    # dispatch_task is imported lazily inside the function — patch at source.
    dispatched: list[int] = []
    monkeypatch.setattr(
        "app.tasks.dispatch.dispatch_task",
        lambda task, doc_id: dispatched.append(doc_id),
    )

    recover_stuck_pending_dispatches(db_session, max_age_seconds=0)

    assert doc.id not in dispatched, (
        "ENRICH was dispatched even though BATCH_ANALYSIS is still PENDING — "
        "this is the premature dispatch that strands docs via gate-block-skip."
    )


@pytest.mark.unit
def test_recover_dispatches_enrich_when_batch_analysis_completed(
    db_session, sample_case, monkeypatch
):
    """When BATCH_ANALYSIS is COMPLETED and ENRICH is PENDING, the recovery sweep
    SHOULD dispatch enrich — this is the normal stuck-pending path."""
    batch = _make_batch(db_session, sample_case.id)
    doc = _make_doc_with_batch(db_session, sample_case.id, batch.id)

    _upsert_stage(db_session, doc.id, "extract", "completed")
    _upsert_stage(db_session, doc.id, "metadata", "completed")
    _upsert_stage(db_session, doc.id, "embeddings", "completed")
    _upsert_stage(db_session, doc.id, "batch_analysis", "completed")
    _upsert_stage(db_session, doc.id, "enrich", "pending")

    dispatched: list[int] = []
    monkeypatch.setattr(
        "app.tasks.dispatch.dispatch_task",
        lambda task, doc_id: dispatched.append(doc_id),
    )

    recover_stuck_pending_dispatches(db_session, max_age_seconds=0)

    assert doc.id in dispatched, (
        "ENRICH was NOT dispatched even though BATCH_ANALYSIS is COMPLETED and ENRICH is PENDING."
    )


# ---------------------------------------------------------------------------
# Fix B: enrich gate-block resets ENRICH to PENDING (not SKIPPED)
# ---------------------------------------------------------------------------


@pytest.mark.unit
def test_enrich_gate_block_resets_to_pending_not_skipped(
    db_session, db_session_factory, sample_case, monkeypatch
):
    """When enrich_document_task fires prematurely and BATCH_ANALYSIS is not
    terminal, it must reset ENRICH to PENDING (not mark SKIPPED). A SKIPPED
    row cannot be reclaimed by _enrich_if_pending's CAS."""
    batch = _make_batch(db_session, sample_case.id)
    doc = _make_doc_with_batch(db_session, sample_case.id, batch.id)
    doc_id = doc.id

    _upsert_stage(db_session, doc.id, "extract", "completed")
    _upsert_stage(db_session, doc.id, "metadata", "completed")
    _upsert_stage(db_session, doc.id, "batch_analysis", "pending")

    # The task's CAS sets ENRICH to RUNNING before dispatch. Simulate that:
    _upsert_stage(db_session, doc.id, "enrich", "running")

    # The task calls db.close() in finally, which would detach db_session.
    # Return a fresh session for each call so the task can close it without
    # breaking the test fixture session.
    monkeypatch.setattr("app.dependencies.get_db_session", db_session_factory)
    monkeypatch.setattr(
        "app.services.intelligence.document_enricher.enrich", lambda doc_id: None
    )

    from app.tasks.enrich_document import enrich_document_task

    result = enrich_document_task(doc_id)

    assert result["status"] == "deferred"
    assert result["reason"] == "batch_analysis_not_completed"

    # ENRICH must be PENDING (not SKIPPED) so _enrich_if_pending can reclaim it.
    row = db_session.execute(
        text(
            "SELECT status, reason FROM document_pipeline_stages "
            "WHERE document_id=:d AND stage='enrich'"
        ),
        {"d": doc_id},
    ).one()
    assert row.status == "pending", (
        f"ENRICH should be PENDING after gate-block deferral, got {row.status!r}. "
        "A SKIPPED row cannot be reclaimed by _enrich_if_pending's CAS."
    )
    assert row.reason is None


# ---------------------------------------------------------------------------
# Fix C: recover_stuck_batches must not clear claim for already-completed batch
# ---------------------------------------------------------------------------


@pytest.mark.unit
def test_recover_stuck_batches_skips_already_completed_batch(db_session, sample_case):
    """If batch_analysis already completed, recover_stuck_batches must NOT clear
    analysis_queued_at — doing so re-opens the CAS and lets stale
    process_document_task replays trigger repeated analyze_batch runs (ib-0001 loop)."""
    old_ts = datetime.now(UTC) - timedelta(hours=2)
    batch = _make_batch(db_session, sample_case.id, analysis_queued_at=old_ts)
    doc = _make_doc_with_batch(db_session, sample_case.id, batch.id)
    db_session.commit()

    # Mark batch_analysis as completed for this doc.
    _upsert_stage(db_session, doc.id, "batch_analysis", "completed")

    recover_stuck_batches(db_session, max_age_seconds=0)

    db_session.refresh(batch)
    assert batch.analysis_queued_at is not None, (
        "recover_stuck_batches cleared analysis_queued_at for a batch whose "
        "batch_analysis is already COMPLETED — this would allow stale tasks to re-fire it."
    )


@pytest.mark.unit
def test_recover_stuck_batches_clears_genuinely_stuck_batch(db_session, sample_case):
    """If batch_analysis is NOT terminal and the batch has been stuck > threshold,
    recover_stuck_batches SHOULD clear the claim so it can be re-triggered."""
    old_ts = datetime.now(UTC) - timedelta(hours=2)
    batch = _make_batch(db_session, sample_case.id, analysis_queued_at=old_ts)
    doc = _make_doc_with_batch(db_session, sample_case.id, batch.id)
    db_session.commit()

    # batch_analysis is still pending — not started yet.
    _upsert_stage(db_session, doc.id, "batch_analysis", "pending")

    recover_stuck_batches(db_session, max_age_seconds=0)

    db_session.refresh(batch)
    assert batch.analysis_queued_at is None, (
        "recover_stuck_batches did NOT clear analysis_queued_at for a genuinely "
        "stuck batch (batch_analysis never ran)."
    )


# ---------------------------------------------------------------------------
# Fix D: claim_batch_for_analysis refuses when batch_analysis already terminal
# ---------------------------------------------------------------------------


@pytest.mark.unit
def test_claim_batch_for_analysis_refuses_when_already_completed(
    db_session, sample_case
):
    """claim_batch_for_analysis must return False when batch_analysis is already
    in a terminal state for any doc in the batch. This prevents stale
    process_document_task messages from re-firing the analyzer after
    recover_stuck_batches (or any other path) clears analysis_queued_at."""
    from app.services.intelligence.orchestrator import claim_batch_for_analysis

    batch = _make_batch(db_session, sample_case.id)
    doc = _make_doc_with_batch(db_session, sample_case.id, batch.id)
    db_session.commit()

    # Mark extract + metadata + batch_analysis all completed (batch already done).
    _upsert_stage(db_session, doc.id, "extract", "completed")
    _upsert_stage(db_session, doc.id, "metadata", "completed")
    _upsert_stage(db_session, doc.id, "batch_analysis", "completed")

    # analysis_queued_at is NULL — simulates the state after recover_stuck_batches
    # cleared it incorrectly or after any other path reset it.
    assert batch.analysis_queued_at is None

    result = claim_batch_for_analysis(batch.id, db_session)

    assert result is False, (
        "claim_batch_for_analysis returned True for a batch whose batch_analysis "
        "is already COMPLETED — this would re-fire the analyzer on an already-done batch."
    )
    db_session.refresh(batch)
    assert batch.analysis_queued_at is None


@pytest.mark.unit
def test_claim_batch_for_analysis_succeeds_when_not_yet_analyzed(
    db_session, sample_case
):
    """claim_batch_for_analysis must succeed for a batch that is ready but hasn't
    been analyzed yet — the idempotency guard must not block legitimate claims."""
    from app.services.intelligence.orchestrator import claim_batch_for_analysis

    batch = _make_batch(db_session, sample_case.id)
    doc = _make_doc_with_batch(db_session, sample_case.id, batch.id)
    db_session.commit()

    _upsert_stage(db_session, doc.id, "extract", "completed")
    _upsert_stage(db_session, doc.id, "metadata", "completed")
    _upsert_stage(db_session, doc.id, "batch_analysis", "pending")

    result = claim_batch_for_analysis(batch.id, db_session)

    assert result is True
    db_session.refresh(batch)
    assert batch.analysis_queued_at is not None


# ---------------------------------------------------------------------------
# Fix E: recover_stranded_gate_skipped unstrands affected docs
# ---------------------------------------------------------------------------


@pytest.mark.unit
def test_recover_stranded_gate_skipped_resets_enrich_and_dispatches(
    db_session, sample_case, monkeypatch
):
    """Docs stranded by the gate-block-skip race (ENRICH=SKIPPED with gate reason,
    BATCH_ANALYSIS=COMPLETED) should be found, reset to PENDING, and dispatched."""
    batch = _make_batch(db_session, sample_case.id)
    doc = _make_doc_with_batch(db_session, sample_case.id, batch.id)
    db_session.commit()

    # Simulate post-race state: batch_analysis completed, enrich gate-skipped.
    _upsert_stage(db_session, doc.id, "extract", "completed")
    _upsert_stage(db_session, doc.id, "metadata", "completed")
    _upsert_stage(db_session, doc.id, "batch_analysis", "completed")
    _upsert_stage(
        db_session, doc.id, "enrich", "skipped", reason="batch_analysis_not_completed"
    )
    # Cascade-skipped downstream (as extract_claims and others would do).
    _upsert_stage(
        db_session, doc.id, "claims", "skipped", reason="enrich_not_completed"
    )
    _upsert_stage(
        db_session, doc.id, "relationships", "skipped", reason="enrich_not_completed"
    )
    _upsert_stage(
        db_session, doc.id, "entities", "skipped", reason="enrich_not_completed"
    )

    dispatched: list[int] = []
    monkeypatch.setattr(
        "app.tasks.dispatch.dispatch_task",
        lambda task, doc_id: dispatched.append(doc_id),
    )

    result = recover_stranded_gate_skipped(db_session)

    assert result["docs_recovered"] == 1
    assert doc.id in result["doc_ids"]
    assert doc.id in dispatched

    # ENRICH must be RUNNING (claimed by recover_stranded_gate_skipped CAS).
    db_session.expire_all()
    enrich_row = db_session.execute(
        text(
            "SELECT status FROM document_pipeline_stages WHERE document_id=:d AND stage='enrich'"
        ),
        {"d": doc.id},
    ).one()
    assert enrich_row.status == "running"

    # Downstream stages (claims, relationships, entities) must be PENDING.
    for stage in ("claims", "relationships", "entities"):
        row = db_session.execute(
            text(
                "SELECT status, reason FROM document_pipeline_stages WHERE document_id=:d AND stage=:s"
            ),
            {"d": doc.id, "s": stage},
        ).one()
        assert row.status == "pending", f"{stage} should be PENDING after recovery"
        assert row.reason is None


@pytest.mark.unit
def test_recover_stranded_gate_skipped_ignores_policy_skipped(
    db_session, sample_case, monkeypatch
):
    """ENRICH=SKIPPED with a policy reason (not a gate-block reason) must NOT be
    recovered — those are intentional and should stay SKIPPED."""
    batch = _make_batch(db_session, sample_case.id)
    doc = _make_doc_with_batch(db_session, sample_case.id, batch.id)
    db_session.commit()

    _upsert_stage(db_session, doc.id, "batch_analysis", "completed")
    # Policy skip — not a gate-block reason.
    _upsert_stage(
        db_session, doc.id, "enrich", "skipped", reason="ineligible_tier:administrative"
    )

    dispatched: list[int] = []
    monkeypatch.setattr(
        "app.tasks.dispatch.dispatch_task",
        lambda task, doc_id: dispatched.append(doc_id),
    )

    result = recover_stranded_gate_skipped(db_session)

    assert result["docs_recovered"] == 0
    assert doc.id not in dispatched


@pytest.mark.unit
def test_recover_stranded_gate_skipped_ignores_batch_analysis_not_done(
    db_session, sample_case, monkeypatch
):
    """If ENRICH is gate-skipped but BATCH_ANALYSIS is still PENDING (not yet done),
    the doc is not stranded — it will recover naturally when batch_analysis completes.
    recover_stranded_gate_skipped must not touch these docs."""
    batch = _make_batch(db_session, sample_case.id)
    doc = _make_doc_with_batch(db_session, sample_case.id, batch.id)
    db_session.commit()

    _upsert_stage(db_session, doc.id, "batch_analysis", "pending")
    _upsert_stage(
        db_session, doc.id, "enrich", "skipped", reason="batch_analysis_not_completed"
    )

    dispatched: list[int] = []
    monkeypatch.setattr(
        "app.tasks.dispatch.dispatch_task",
        lambda task, doc_id: dispatched.append(doc_id),
    )

    result = recover_stranded_gate_skipped(db_session)

    assert result["docs_recovered"] == 0
    assert doc.id not in dispatched
