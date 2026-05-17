"""Tests for the pipeline_status stage registry and atomic-commit semantics."""

import pytest

from app.models.enums import PipelineStage, PipelineState, StageStatus

# ---------------------------------------------------------------------------
# Registry completeness
# ---------------------------------------------------------------------------


@pytest.mark.unit
def test_registry_covers_every_stage():
    """Every PipelineStage member must have a STAGE_REGISTRY entry.

    The module-level guard in pipeline_status already raises on import
    if an entry is missing; this test makes the contract explicit and
    gives a clear failure message when a new enum member is added.
    """
    from app.services.pipeline_status import STAGE_REGISTRY

    assert set(STAGE_REGISTRY.keys()) == set(PipelineStage)


@pytest.mark.unit
def test_stage_order_is_unique():
    """No two stages share the same order value."""
    from app.services.pipeline_status import STAGE_REGISTRY

    orders = [spec.order for spec in STAGE_REGISTRY.values()]
    assert len(orders) == len(set(orders))


@pytest.mark.unit
def test_retry_tasks_are_non_empty():
    """Every stage spec has a non-empty retry_task dotted path."""
    from app.services.pipeline_status import STAGE_REGISTRY

    for stage, spec in STAGE_REGISTRY.items():
        assert spec.retry_task, f"STAGE_REGISTRY[{stage}].retry_task is empty"


# ---------------------------------------------------------------------------
# Atomic commit semantics
# ---------------------------------------------------------------------------


@pytest.mark.unit
def test_mark_completed_respects_commit_false(db_session):
    """mark_completed(commit=False) stages the change but does not flush to DB.

    Rolling back the session after calling mark_completed(commit=False) leaves
    the pipeline_stages row unchanged — the stage is still RUNNING, not COMPLETED.
    """
    from sqlalchemy import text

    from app.models.database import Case, Document
    from app.models.enums import CaseStatus, Jurisdiction, OriginatorType
    from app.services.pipeline_status import initialize, mark_completed, mark_started

    case = Case(
        id="_T3", title="T", status=CaseStatus.INTAKE, jurisdiction=Jurisdiction.DE
    )
    db_session.add(case)
    db_session.commit()

    doc = Document(
        title="x",
        content="x",
        case_id="_T3",
        originator_type=OriginatorType.COURT,
    )
    initialize(doc, batched=False)
    db_session.add(doc)
    db_session.commit()
    db_session.refresh(doc)

    mark_started(doc.id, PipelineStage.EMBEDDINGS, db_session)

    # Call mark_completed without committing
    mark_completed(doc.id, PipelineStage.EMBEDDINGS, db_session, commit=False)

    # Rolling back undoes the pending stage update
    db_session.rollback()

    # The row in the DB should still show RUNNING (from mark_started which committed)
    row = db_session.execute(
        text("SELECT pipeline_stages FROM documents WHERE id = :id"),
        {"id": doc.id},
    ).fetchone()
    import json

    stages = json.loads(row[0])
    assert stages[PipelineStage.EMBEDDINGS.value]["status"] == StageStatus.RUNNING.value


@pytest.mark.unit
def test_sequential_stage_updates_both_survive(db_session):
    """Updating two different stages sequentially leaves both updates persisted.

    Verifies that the read-modify-write in _update_stage does not lose earlier
    stage data when a later stage is written: each call patches exactly its own
    key inside pipeline_stages without clobbering adjacent keys.
    """
    from app.models.database import Case, Document
    from app.models.enums import CaseStatus, Jurisdiction, OriginatorType
    from app.services.pipeline_status import initialize, mark_completed, mark_started

    case = Case(
        id="_T4", title="T", status=CaseStatus.INTAKE, jurisdiction=Jurisdiction.DE
    )
    db_session.add(case)
    db_session.commit()

    doc = Document(
        title="x",
        content="x",
        case_id="_T4",
        originator_type=OriginatorType.COURT,
    )
    initialize(doc, batched=False)
    db_session.add(doc)
    db_session.commit()

    mark_started(doc.id, PipelineStage.CLAIMS, db_session)
    mark_completed(doc.id, PipelineStage.CLAIMS, db_session)
    mark_started(doc.id, PipelineStage.ENTITIES, db_session)
    mark_completed(doc.id, PipelineStage.ENTITIES, db_session)

    db_session.refresh(doc)
    stages = doc.pipeline_stages or {}
    assert (
        stages.get(PipelineStage.CLAIMS.value, {}).get("status")
        == StageStatus.COMPLETED.value
    )
    assert (
        stages.get(PipelineStage.ENTITIES.value, {}).get("status")
        == StageStatus.COMPLETED.value
    )


# ---------------------------------------------------------------------------
# mark_failed_with_cascade
# ---------------------------------------------------------------------------


@pytest.mark.unit
def test_mark_failed_with_cascade_propagates(db_session):
    """mark_failed_with_cascade(EXTRACT) must fail EXTRACT and all per-doc downstream
    stages (METADATA, ENRICH, RELATIONSHIPS, CLAIMS, ENTITIES)
    but leave BATCH_ANALYSIS (not in EXTRACT's downstream) as PENDING.
    """
    from app.models.database import Case, Document
    from app.models.enums import CaseStatus, Jurisdiction, OriginatorType
    from app.services.pipeline_status import initialize, mark_failed_with_cascade

    case = Case(
        id="_T5", title="T", status=CaseStatus.INTAKE, jurisdiction=Jurisdiction.DE
    )
    db_session.add(case)
    db_session.commit()

    doc = Document(
        title="x",
        content="x",
        case_id="_T5",
        originator_type=OriginatorType.COURT,
    )
    initialize(doc, batched=True)
    db_session.add(doc)
    db_session.commit()
    db_session.refresh(doc)

    mark_failed_with_cascade(doc.id, PipelineStage.EXTRACT, db_session, error="boom")

    db_session.refresh(doc)
    stages = doc.pipeline_stages

    assert stages[PipelineStage.EXTRACT.value]["status"] == StageStatus.FAILED.value
    assert stages[PipelineStage.METADATA.value]["status"] == StageStatus.FAILED.value
    assert stages[PipelineStage.ENRICH.value]["status"] == StageStatus.FAILED.value
    assert (
        stages[PipelineStage.RELATIONSHIPS.value]["status"] == StageStatus.FAILED.value
    )
    assert stages[PipelineStage.CLAIMS.value]["status"] == StageStatus.FAILED.value
    assert stages[PipelineStage.ENTITIES.value]["status"] == StageStatus.FAILED.value
    # BATCH_ANALYSIS is not in EXTRACT's downstream — must stay PENDING
    assert (
        stages[PipelineStage.BATCH_ANALYSIS.value]["status"]
        == StageStatus.PENDING.value
    )
    # Downstream error messages reference the upstream stage
    assert "extract" in stages[PipelineStage.METADATA.value]["error"]


# ---------------------------------------------------------------------------
# aggregate_pipeline_summary
# ---------------------------------------------------------------------------


@pytest.mark.unit
def test_aggregate_pipeline_summary_counts_by_state():
    """aggregate_pipeline_summary returns correct per-state counts."""
    from app.services.pipeline_status import aggregate_pipeline_summary

    all_stages = {
        s.value: {"status": StageStatus.COMPLETED.value} for s in PipelineStage
    }
    result = aggregate_pipeline_summary([all_stages, all_stages])
    assert result["total"] == 2
    assert result.get(PipelineState.COMPLETED.value, 0) == 2


@pytest.mark.unit
def test_aggregate_pipeline_summary_empty():
    from app.services.pipeline_status import aggregate_pipeline_summary

    result = aggregate_pipeline_summary([])
    assert result["total"] == 0


# ---------------------------------------------------------------------------
# RETRYING stage status — keeps polling alive across Celery retries
# ---------------------------------------------------------------------------


@pytest.mark.unit
def test_compute_overall_state_treats_retrying_as_running():
    """A retrying stage must roll up to PipelineState.RUNNING so polling
    templates (gated on pending|running) keep refreshing through the
    retry countdown."""
    from app.services.pipeline_status import compute_overall_state

    stages = {
        "extract": {"status": StageStatus.RETRYING.value},
        "metadata": {"status": StageStatus.PENDING.value},
    }
    assert compute_overall_state(stages) == PipelineState.RUNNING

    # Retrying mixed with running still rolls up to running.
    stages = {
        "extract": {"status": StageStatus.RUNNING.value},
        "metadata": {"status": StageStatus.RETRYING.value},
    }
    assert compute_overall_state(stages) == PipelineState.RUNNING


@pytest.mark.unit
def test_mark_retrying_writes_record_shape(db_session):
    """mark_retrying writes status=retrying plus error, attempt, max_attempts, next_at."""
    from app.models.database import Case, Document
    from app.models.enums import CaseStatus, Jurisdiction, OriginatorType
    from app.services.pipeline_status import (
        initialize,
        mark_retrying,
        mark_started,
    )

    case = Case(
        id="_TR_R1", title="T", status=CaseStatus.INTAKE, jurisdiction=Jurisdiction.DE
    )
    db_session.add(case)
    db_session.commit()

    doc = Document(
        title="x",
        content="x",
        case_id="_TR_R1",
        originator_type=OriginatorType.COURT,
    )
    initialize(doc, batched=False)
    db_session.add(doc)
    db_session.commit()

    mark_started(doc.id, PipelineStage.EXTRACT, db_session)
    mark_retrying(
        doc.id,
        PipelineStage.EXTRACT,
        db_session,
        error="boom",
        attempt=2,
        max_attempts=3,
        next_at="2026-05-06T18:32:11+00:00",
    )

    db_session.refresh(doc)
    rec = doc.pipeline_stages[PipelineStage.EXTRACT.value]
    assert rec["status"] == StageStatus.RETRYING.value
    assert rec["error"] == "boom"
    assert rec["attempt"] == 2
    assert rec["max_attempts"] == 3
    assert rec["next_at"] == "2026-05-06T18:32:11+00:00"
    # Aggregate state is RUNNING — polling must keep firing.
    assert doc.pipeline_state.value == PipelineState.RUNNING.value


@pytest.mark.unit
def test_mark_started_clears_retry_bookkeeping(db_session):
    """When the next attempt actually runs, mark_started must wipe
    attempt/max_attempts/next_at left over from a prior RETRYING record so
    the UI doesn't show stale countdown info next to a clean RUNNING dot."""
    from app.models.database import Case, Document
    from app.models.enums import CaseStatus, Jurisdiction, OriginatorType
    from app.services.pipeline_status import (
        initialize,
        mark_retrying,
        mark_started,
    )

    case = Case(
        id="_TR_R2", title="T", status=CaseStatus.INTAKE, jurisdiction=Jurisdiction.DE
    )
    db_session.add(case)
    db_session.commit()

    doc = Document(
        title="x",
        content="x",
        case_id="_TR_R2",
        originator_type=OriginatorType.COURT,
    )
    initialize(doc, batched=False)
    db_session.add(doc)
    db_session.commit()

    mark_started(doc.id, PipelineStage.EXTRACT, db_session)
    mark_retrying(
        doc.id,
        PipelineStage.EXTRACT,
        db_session,
        error="boom",
        attempt=1,
        max_attempts=3,
        next_at="2026-05-06T18:32:11+00:00",
    )
    # Next attempt actually starts.
    mark_started(doc.id, PipelineStage.EXTRACT, db_session)

    db_session.refresh(doc)
    rec = doc.pipeline_stages[PipelineStage.EXTRACT.value]
    assert rec["status"] == StageStatus.RUNNING.value
    assert "attempt" not in rec
    assert "max_attempts" not in rec
    assert "next_at" not in rec


@pytest.mark.unit
def test_schedule_retry_computes_next_at_from_countdown(db_session):
    """schedule_retry is the canonical helper used at every Celery `self.retry`
    site — it computes next_at = now + countdown and forwards to mark_retrying."""
    from datetime import UTC, datetime, timedelta

    from app.models.database import Case, Document
    from app.models.enums import CaseStatus, Jurisdiction, OriginatorType
    from app.services.pipeline_status import (
        initialize,
        mark_started,
        schedule_retry,
    )

    case = Case(
        id="_TR_R3", title="T", status=CaseStatus.INTAKE, jurisdiction=Jurisdiction.DE
    )
    db_session.add(case)
    db_session.commit()

    doc = Document(
        title="x",
        content="x",
        case_id="_TR_R3",
        originator_type=OriginatorType.COURT,
    )
    initialize(doc, batched=False)
    db_session.add(doc)
    db_session.commit()

    mark_started(doc.id, PipelineStage.EXTRACT, db_session)
    before = datetime.now(UTC)
    schedule_retry(
        doc.id,
        PipelineStage.EXTRACT,
        db_session,
        error="timeout",
        attempt=1,
        max_attempts=3,
        countdown=60,
    )
    after = datetime.now(UTC)

    db_session.refresh(doc)
    rec = doc.pipeline_stages[PipelineStage.EXTRACT.value]
    assert rec["status"] == StageStatus.RETRYING.value
    next_at = datetime.fromisoformat(rec["next_at"])
    # next_at should be ~60s from now, between before+60 and after+60
    assert (
        before + timedelta(seconds=60)
        <= next_at
        <= after + timedelta(seconds=60, microseconds=1)
    )


@pytest.mark.unit
def test_recover_orphaned_running_stages_resets_retrying(db_session):
    """A worker that died between mark_retrying and the next Celery attempt
    leaves the stage stuck in RETRYING forever. App-startup recovery must
    treat RETRYING the same as RUNNING — reset to PENDING and re-dispatch."""
    from app.models.database import Case, Document
    from app.models.enums import CaseStatus, Jurisdiction, OriginatorType
    from app.services.pipeline_status import (
        initialize,
        recover_orphaned_running_stages,
    )

    case = Case(
        id="_TR_R4", title="T", status=CaseStatus.INTAKE, jurisdiction=Jurisdiction.DE
    )
    db_session.add(case)
    db_session.commit()

    doc = Document(
        title="stuck-retrying.pdf",
        content="x",
        case_id="_TR_R4",
        originator_type=OriginatorType.UNKNOWN,
    )
    initialize(doc, batched=False)
    stages = dict(doc.pipeline_stages)
    stages[PipelineStage.EXTRACT.value] = {
        "status": StageStatus.RETRYING.value,
        "error": "boom",
        "attempt": 2,
        "max_attempts": 3,
        "next_at": "2026-05-06T18:32:11+00:00",
    }
    doc.pipeline_stages = stages
    doc.pipeline_state = "running"  # retrying rolls up to running
    db_session.add(doc)
    db_session.commit()

    result = recover_orphaned_running_stages(db_session)

    assert result["docs_reset"] == 1
    assert result["stages_reset"] == 1

    db_session.refresh(doc)
    rec = doc.pipeline_stages[PipelineStage.EXTRACT.value]
    assert rec["status"] == StageStatus.PENDING.value
    # Retry bookkeeping should be wiped on reset.
    assert "attempt" not in rec
    assert "max_attempts" not in rec
    assert "next_at" not in rec


# ---------------------------------------------------------------------------
# recover_stuck_pending_dispatches — EAGER+reload hazard recovery
# ---------------------------------------------------------------------------


@pytest.mark.unit
def test_recover_stuck_pending_redispatches_old_pending_doc(db_session, monkeypatch):
    """Doc with pipeline_state=pending + extract.status=pending older than the
    threshold must be re-dispatched. Reproduces the ib-0007 stuck-upload case
    where uvicorn --reload killed the daemon thread before mark_started ran."""
    from datetime import UTC, datetime, timedelta

    from app.models.database import Case, Document
    from app.models.enums import CaseStatus, Jurisdiction, OriginatorType
    from app.services.pipeline_status import (
        initialize,
        recover_stuck_pending_dispatches,
    )

    case = Case(
        id="_TR1", title="T", status=CaseStatus.INTAKE, jurisdiction=Jurisdiction.DE
    )
    db_session.add(case)
    db_session.commit()

    doc = Document(
        title="stuck.pdf",
        content=None,  # Docling never ran
        case_id="_TR1",
        originator_type=OriginatorType.UNKNOWN,
        ingest_date=datetime.now(UTC).replace(tzinfo=None) - timedelta(minutes=5),
    )
    initialize(doc, batched=False)
    db_session.add(doc)
    db_session.commit()
    db_session.refresh(doc)

    captured: list[int] = []

    def fake_dispatch(task, *args, **kwargs):
        # task is a Celery task object; we only assert it was called with the doc id
        captured.append(args[0] if args else kwargs.get("doc_id"))

    monkeypatch.setattr("app.tasks.dispatch.dispatch_task", fake_dispatch)

    result = recover_stuck_pending_dispatches(db_session)

    assert result["docs_redispatched"] == 1
    assert result["doc_ids"] == [doc.id]
    assert captured == [doc.id]


@pytest.mark.unit
def test_recover_stuck_pending_skips_recent_pending_doc(db_session, monkeypatch):
    """A pending doc inside the age threshold (recently uploaded) must NOT be
    re-dispatched — the upload's own dispatch is still in flight."""
    from datetime import UTC, datetime

    from app.models.database import Case, Document
    from app.models.enums import CaseStatus, Jurisdiction, OriginatorType
    from app.services.pipeline_status import (
        initialize,
        recover_stuck_pending_dispatches,
    )

    case = Case(
        id="_TR2", title="T", status=CaseStatus.INTAKE, jurisdiction=Jurisdiction.DE
    )
    db_session.add(case)
    db_session.commit()

    doc = Document(
        title="fresh.pdf",
        content=None,
        case_id="_TR2",
        originator_type=OriginatorType.UNKNOWN,
        ingest_date=datetime.now(UTC).replace(tzinfo=None),  # just now
    )
    initialize(doc, batched=False)
    db_session.add(doc)
    db_session.commit()

    captured: list[int] = []

    def fake_dispatch(task, *args, **kwargs):
        captured.append(args[0] if args else kwargs.get("doc_id"))

    monkeypatch.setattr("app.tasks.dispatch.dispatch_task", fake_dispatch)

    result = recover_stuck_pending_dispatches(db_session, max_age_seconds=60)

    assert result["docs_redispatched"] == 0
    assert result["doc_ids"] == []
    assert captured == []


@pytest.mark.unit
def test_recover_stuck_pending_resumes_partial_pipeline(db_session, monkeypatch):
    """A doc with EXTRACT completed but METADATA pending (and old) must be
    re-dispatched — the cascade got killed mid-flight, not before it started.
    Reproduces the ib-0007 follow-up bug where recovery silently skipped
    docs whose EXTRACT had already finished."""
    from datetime import UTC, datetime, timedelta

    from app.models.database import Case, Document
    from app.models.enums import CaseStatus, Jurisdiction, OriginatorType
    from app.services.pipeline_status import (
        PipelineStage,
        StageStatus,
        initialize,
        recover_stuck_pending_dispatches,
    )

    case = Case(
        id="_TR3", title="T", status=CaseStatus.INTAKE, jurisdiction=Jurisdiction.DE
    )
    db_session.add(case)
    db_session.commit()

    doc = Document(
        title="extracted.pdf",
        content="some content",
        case_id="_TR3",
        originator_type=OriginatorType.UNKNOWN,
        ingest_date=datetime.now(UTC).replace(tzinfo=None) - timedelta(minutes=5),
    )
    initialize(doc, batched=False)
    stages = dict(doc.pipeline_stages)
    stages[PipelineStage.EXTRACT.value] = {"status": StageStatus.COMPLETED.value}
    # METADATA + downstream remain pending.
    doc.pipeline_stages = stages
    doc.pipeline_state = "partial"  # extract done, downstream pending
    db_session.add(doc)
    db_session.commit()

    captured: list[tuple] = []

    def fake_dispatch(task, *args, **kwargs):
        captured.append((task, args, kwargs))

    monkeypatch.setattr("app.tasks.dispatch.dispatch_task", fake_dispatch)

    result = recover_stuck_pending_dispatches(db_session)

    # Recovery must dispatch process_document_task (METADATA's retry_task) so
    # the task can resume from METADATA — it skips EXTRACT internally if done.
    assert result["docs_redispatched"] == 1
    assert result["doc_ids"] == [doc.id]
    assert len(captured) == 1
    task, args, _ = captured[0]
    assert args == (doc.id,)
    # process_document_task is the retry_task for both EXTRACT and METADATA.
    assert task.name == "app.tasks.document_processing.process_document_task"


@pytest.mark.unit
def test_recover_stuck_pending_skips_running_stage(db_session, monkeypatch):
    """A doc with a RUNNING stage must NOT be re-dispatched — the running-state
    recovery owns that case (and runs first in lifespan)."""
    from datetime import UTC, datetime, timedelta

    from app.models.database import Case, Document
    from app.models.enums import CaseStatus, Jurisdiction, OriginatorType
    from app.services.pipeline_status import (
        PipelineStage,
        StageStatus,
        initialize,
        recover_stuck_pending_dispatches,
    )

    case = Case(
        id="_TR4", title="T", status=CaseStatus.INTAKE, jurisdiction=Jurisdiction.DE
    )
    db_session.add(case)
    db_session.commit()

    doc = Document(
        title="running.pdf",
        content="x",
        case_id="_TR4",
        originator_type=OriginatorType.UNKNOWN,
        ingest_date=datetime.now(UTC).replace(tzinfo=None) - timedelta(minutes=5),
    )
    initialize(doc, batched=False)
    stages = dict(doc.pipeline_stages)
    stages[PipelineStage.METADATA.value] = {"status": StageStatus.RUNNING.value}
    doc.pipeline_stages = stages
    doc.pipeline_state = "running"
    db_session.add(doc)
    db_session.commit()

    captured: list[int] = []

    def fake_dispatch(task, *args, **kwargs):
        captured.append(args[0] if args else kwargs.get("doc_id"))

    monkeypatch.setattr("app.tasks.dispatch.dispatch_task", fake_dispatch)

    result = recover_stuck_pending_dispatches(db_session)

    assert result["docs_redispatched"] == 0
    assert captured == []


# ---------------------------------------------------------------------------
# recover_unclaimed_ready_batches — closes the gap where per-stage retries
# leave an IngestBatch with analysis_queued_at IS NULL but every doc ready.
# ---------------------------------------------------------------------------


def _seed_batch_with_docs(db_session, *, case_id: str, doc_stages: list[dict]):
    """Create an IngestBatch + Documents whose pipeline_stages match `doc_stages`.

    Each entry in `doc_stages` is a dict mapping PipelineStage.value → status string.
    Stages not listed default to PENDING via initialize().
    """
    from app.models.database import Document, IngestBatch
    from app.models.enums import IngestBatchSourceType, OriginatorType
    from app.services.pipeline_status import initialize

    batch = IngestBatch(
        source_type=IngestBatchSourceType.MANUAL,
        case_id=None,
        analysis_queued_at=None,
    )
    db_session.add(batch)
    db_session.flush()

    docs = []
    for i, stage_overrides in enumerate(doc_stages):
        doc = Document(
            title=f"d{i}.pdf",
            content="x",
            case_id=case_id,
            originator_type=OriginatorType.UNKNOWN,
            ingest_batch_id=batch.id,
        )
        initialize(doc, batched=True)
        stages = dict(doc.pipeline_stages)
        for stage_key, status in stage_overrides.items():
            stages[stage_key] = {"status": status}
        doc.pipeline_stages = stages
        db_session.add(doc)
        docs.append(doc)
    db_session.commit()
    db_session.refresh(batch)
    for d in docs:
        db_session.refresh(d)
    return batch, docs


@pytest.mark.unit
def test_recover_unclaimed_ready_batches_claims_and_dispatches(db_session, monkeypatch):
    """Batch where every doc has extract+metadata completed and batch_analysis
    still pending must be claimed (analysis_queued_at set) and analyze_batch_task
    dispatched."""
    from app.models.database import Case
    from app.models.enums import CaseStatus, Jurisdiction
    from app.services.pipeline_status import (
        initialize,  # noqa: F401  — used inside _seed helper
        recover_unclaimed_ready_batches,
    )

    case = Case(
        id="_RU1", title="T", status=CaseStatus.INTAKE, jurisdiction=Jurisdiction.DE
    )
    db_session.add(case)
    db_session.commit()

    ready = {
        PipelineStage.EXTRACT.value: StageStatus.COMPLETED.value,
        PipelineStage.METADATA.value: StageStatus.COMPLETED.value,
    }
    batch, _docs = _seed_batch_with_docs(
        db_session, case_id="_RU1", doc_stages=[ready, ready]
    )

    captured: list[int] = []

    class _FakeDelay:
        def delay(self, batch_id):
            captured.append(batch_id)

    monkeypatch.setattr("app.tasks.analyze_batch.analyze_batch_task", _FakeDelay())

    result = recover_unclaimed_ready_batches(db_session)

    assert result["batches_dispatched"] == 1
    assert result["batch_ids"] == [batch.id]
    assert captured == [batch.id]

    db_session.refresh(batch)
    assert batch.analysis_queued_at is not None


@pytest.mark.unit
def test_recover_unclaimed_ready_batches_skips_when_metadata_pending(
    db_session, monkeypatch
):
    """Readiness must be honoured: if any doc still has metadata pending, the
    claim's NOT EXISTS guard rejects, no dispatch happens, analysis_queued_at
    stays NULL. Proves we delegate readiness to claim_batch_for_analysis()."""
    from app.models.database import Case
    from app.models.enums import CaseStatus, Jurisdiction
    from app.services.pipeline_status import (
        initialize,  # noqa: F401
        recover_unclaimed_ready_batches,
    )

    case = Case(
        id="_RU2", title="T", status=CaseStatus.INTAKE, jurisdiction=Jurisdiction.DE
    )
    db_session.add(case)
    db_session.commit()

    ready = {
        PipelineStage.EXTRACT.value: StageStatus.COMPLETED.value,
        PipelineStage.METADATA.value: StageStatus.COMPLETED.value,
    }
    not_ready = {
        PipelineStage.EXTRACT.value: StageStatus.COMPLETED.value,
        # METADATA still pending — batch should NOT be claimed
    }
    batch, _docs = _seed_batch_with_docs(
        db_session, case_id="_RU2", doc_stages=[ready, not_ready]
    )

    captured: list[int] = []

    class _FakeDelay:
        def delay(self, batch_id):
            captured.append(batch_id)

    monkeypatch.setattr("app.tasks.analyze_batch.analyze_batch_task", _FakeDelay())

    result = recover_unclaimed_ready_batches(db_session)

    assert result["batches_dispatched"] == 0
    assert result["batch_ids"] == []
    assert captured == []

    db_session.refresh(batch)
    assert batch.analysis_queued_at is None


@pytest.mark.unit
def test_recover_unclaimed_ready_batches_ignores_already_claimed(
    db_session, monkeypatch
):
    """A batch with analysis_queued_at already set must be ignored — the
    inline trigger or a prior tick already handled it. Idempotency guard."""
    from datetime import UTC, datetime

    from app.models.database import Case
    from app.models.enums import CaseStatus, Jurisdiction
    from app.services.pipeline_status import (
        initialize,  # noqa: F401
        recover_unclaimed_ready_batches,
    )

    case = Case(
        id="_RU3", title="T", status=CaseStatus.INTAKE, jurisdiction=Jurisdiction.DE
    )
    db_session.add(case)
    db_session.commit()

    ready = {
        PipelineStage.EXTRACT.value: StageStatus.COMPLETED.value,
        PipelineStage.METADATA.value: StageStatus.COMPLETED.value,
    }
    batch, _docs = _seed_batch_with_docs(db_session, case_id="_RU3", doc_stages=[ready])
    claimed_at = datetime.now(UTC).replace(tzinfo=None)
    batch.analysis_queued_at = claimed_at
    db_session.commit()

    captured: list[int] = []

    class _FakeDelay:
        def delay(self, batch_id):
            captured.append(batch_id)

    monkeypatch.setattr("app.tasks.analyze_batch.analyze_batch_task", _FakeDelay())

    result = recover_unclaimed_ready_batches(db_session)

    assert result["batches_dispatched"] == 0
    assert captured == []

    db_session.refresh(batch)
    # Claim timestamp unchanged.
    assert batch.analysis_queued_at == claimed_at


# ---------------------------------------------------------------------------
# retry_on_db_locked
# ---------------------------------------------------------------------------


@pytest.mark.unit
def test_retry_on_db_locked_succeeds_on_first_try(db_session):
    """fn that succeeds immediately: returns value, rollback never called."""
    from unittest.mock import Mock

    from app.services.pipeline_status import retry_on_db_locked

    fn = Mock(return_value="ok")
    db_session.rollback = Mock()

    result = retry_on_db_locked(fn, db_session)

    assert result == "ok"
    fn.assert_called_once()
    db_session.rollback.assert_not_called()


@pytest.mark.unit
def test_retry_on_db_locked_retries_then_succeeds(db_session, monkeypatch):
    """fn raises 'database is locked' once then succeeds: retries, rolls back once."""
    from unittest.mock import Mock

    from sqlalchemy.exc import OperationalError

    from app.services.pipeline_status import retry_on_db_locked

    locked_exc = OperationalError("stmt", {}, Exception("database is locked"))
    fn = Mock(side_effect=[locked_exc, "ok"])
    db_session.rollback = Mock()
    sleep_mock = Mock()
    monkeypatch.setattr("app.services.pipeline_status.time.sleep", sleep_mock)

    result = retry_on_db_locked(fn, db_session)

    assert result == "ok"
    assert fn.call_count == 2
    db_session.rollback.assert_called_once()
    sleep_mock.assert_called_once()
    sleep_args, _ = sleep_mock.call_args
    assert sleep_args[0] > 0


@pytest.mark.unit
def test_retry_on_db_locked_reraises_non_locked_error(db_session):
    """Non-locked OperationalError is re-raised immediately, before rollback."""
    from unittest.mock import Mock

    import pytest as _pytest
    from sqlalchemy.exc import OperationalError

    from app.services.pipeline_status import retry_on_db_locked

    fn = Mock(
        side_effect=OperationalError("stmt", {}, Exception("UNIQUE constraint failed"))
    )
    db_session.rollback = Mock()

    with _pytest.raises(OperationalError):
        retry_on_db_locked(fn, db_session)

    fn.assert_called_once()
    db_session.rollback.assert_not_called()


@pytest.mark.unit
def test_retry_on_db_locked_exhausts_and_reraises(db_session, monkeypatch):
    """fn always raises 'database is locked': exhausts attempts and re-raises."""
    from unittest.mock import Mock

    import pytest as _pytest
    from sqlalchemy.exc import OperationalError

    from app.services.pipeline_status import retry_on_db_locked

    locked_exc = OperationalError("stmt", {}, Exception("database is locked"))
    fn = Mock(side_effect=locked_exc)
    db_session.rollback = Mock()
    sleep_mock = Mock()
    monkeypatch.setattr("app.services.pipeline_status.time.sleep", sleep_mock)

    with _pytest.raises(OperationalError):
        retry_on_db_locked(fn, db_session, attempts=2)

    assert fn.call_count == 2
    assert db_session.rollback.call_count == 2
