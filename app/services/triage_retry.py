"""Service-layer retry helpers shared between the triage and document API modules."""

from __future__ import annotations

import logging

logger = logging.getLogger(__name__)


def dispatch_pipeline_retry(doc_id: int, batch_id: int | None, stage) -> None:
    from app.services.pipeline_status import STAGE_REGISTRY
    from app.tasks.dispatch import dispatch_task

    spec = STAGE_REGISTRY[stage]
    arg = batch_id if spec.dispatch_arg == "batch_id" else doc_id
    if arg is None:
        logger.warning(
            "Cannot dispatch retry for %s — no %s available", stage, spec.dispatch_arg
        )
        return
    dispatch_task(spec.retry_task, arg)


def reset_batch_for_retry(batch, db, *, full: bool = False):
    """Reset pipeline stages for one batch without committing or dispatching.

    Returns (dispatch_items, batch_fallback) on success, or -1 if any stage is
    actively running (caller treats as skip/409). Does NOT commit — the caller
    must commit before calling dispatch_batch_retry with the returned items.

    dispatch_items is a list of (doc_id, batch_id, head_stage | None, needs_emb).
    batch_fallback is True when no per-doc head was found (BATCH_ANALYSIS fallback).
    """
    from sqlalchemy import text as _text

    from app.models.database import Case, Proceeding
    from app.models.enums import (
        DocumentRole,
        IngestBatchStatus,
        PipelineStage,
        StageStatus,
    )
    from app.services.pipeline_status import (
        _STAGE_ORDER,
        compute_overall_state,
        stages_dict,
    )

    # Re-read after any rollback so ORM state reflects DB reality.
    db.refresh(batch)

    # Bail out if any doc has a running stage
    for doc in batch.documents:
        stages = stages_dict(doc)
        if any(
            v.get("status") == StageStatus.RUNNING.value
            for v in stages.values()
            if isinstance(v, dict)
        ):
            return -1

    # Clear the cascade gate so the batch analysis can re-run
    batch.analysis_queued_at = None
    batch.status = IngestBatchStatus.PENDING
    if batch.meta:
        meta = dict(batch.meta)
        meta.pop("reload_fired", None)
        batch.meta = meta

    dispatch_items: list = []

    for doc in batch.documents:
        stages_current = stages_dict(doc)

        new_stages = dict(stages_current)
        for stage in PipelineStage:
            if stage == PipelineStage.EXTRACT and not full:
                continue
            record = stages_current.get(stage.value, {})
            if record.get("status") == StageStatus.SKIPPED.value and record.get(
                "reason"
            ) in (
                "manual upload",
                "no batch (manual upload)",
            ):
                continue
            new_stages[stage.value] = {"status": StageStatus.PENDING.value}

        new_state = compute_overall_state(new_stages)
        doc.pipeline_state = new_state

        for stage in PipelineStage:
            if stage == PipelineStage.EXTRACT and not full:
                continue
            record = stages_current.get(stage.value, {})
            if record.get("status") == StageStatus.SKIPPED.value and record.get(
                "reason"
            ) in (
                "manual upload",
                "no batch (manual upload)",
            ):
                continue
            db.execute(
                _text(
                    "UPDATE document_pipeline_stages SET status='pending', started_at=NULL, "
                    "completed_at=NULL, error=NULL, reason=NULL, attempt=NULL, "
                    "max_attempts=NULL, next_at=NULL "
                    "WHERE document_id = :doc_id AND stage = :stage"
                ),
                {"doc_id": doc.id, "stage": stage.value},
            )
        db.expire(doc, ["stage_rows"])

        doc.role = DocumentRole.STANDALONE
        doc.parent_id = None
        doc.court_relay = False
        doc.attributed_originator = None

        if full:
            if doc.case_id and doc.case_id != "_TRIAGE":
                c = db.query(Case).filter(Case.id == doc.case_id).first()
                if not c or c.is_draft:
                    doc.case_id = "_TRIAGE"
            if doc.proceeding_id:
                p = (
                    db.query(Proceeding)
                    .filter(Proceeding.id == doc.proceeding_id)
                    .first()
                )
                if not p or p.is_draft:
                    doc.proceeding_id = None

        db.execute(
            _text(
                "UPDATE documents SET pipeline_state = :state, case_id = :case_id, "
                "proceeding_id = :proc_id WHERE id = :doc_id"
            ),
            {
                "state": new_state.value,
                "case_id": doc.case_id,
                "proc_id": doc.proceeding_id,
                "doc_id": doc.id,
            },
        )

        head: PipelineStage | None = None
        if full:
            head = PipelineStage.EXTRACT
        else:
            for spec in _STAGE_ORDER:
                if spec.stage in (PipelineStage.EXTRACT, PipelineStage.EMBEDDINGS):
                    continue
                status = new_stages.get(spec.stage.value, {}).get("status")
                if status not in (
                    StageStatus.COMPLETED.value,
                    StageStatus.SKIPPED.value,
                ):
                    head = spec.stage
                    break

        emb_status = new_stages.get(PipelineStage.EMBEDDINGS.value, {}).get("status")
        needs_emb = emb_status not in (
            StageStatus.COMPLETED.value,
            StageStatus.SKIPPED.value,
        )
        dispatch_items.append((doc.id, batch.id, head, needs_emb))

    batch_fallback = not any(head is not None for _, _, head, _ in dispatch_items)
    return dispatch_items, batch_fallback


def dispatch_batch_retry(
    dispatch_items: list, *, batch_fallback: bool, batch_id: int, db
) -> None:
    """Dispatch Celery tasks from a plan built by reset_batch_for_retry.

    Must only be called after the DB transaction for the reset has committed.
    Dispatch is fire-and-forget; errors are logged but not propagated.
    """
    from app.models.enums import PipelineStage

    for doc_id, b_id, head, needs_emb in dispatch_items:
        if head is not None:
            dispatch_pipeline_retry(doc_id, b_id, head)
        if needs_emb:
            dispatch_pipeline_retry(doc_id, b_id, PipelineStage.EMBEDDINGS)

    # Fallback: all per-doc cascade stages are already done but BATCH_ANALYSIS is still
    # pending — e.g. a batch-level retry after docs finished. The cascade won't fire it
    # naturally since no per-doc head task was dispatched.
    if batch_fallback:
        from app.services.intelligence.orchestrator import claim_batch_for_analysis

        if claim_batch_for_analysis(batch_id, db):
            from app.tasks.analyze_batch import analyze_batch_task
            from app.tasks.dispatch import dispatch_task

            dispatch_task(analyze_batch_task, batch_id)
