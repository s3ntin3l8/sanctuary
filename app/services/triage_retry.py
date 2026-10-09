"""Service-layer retry helpers shared between the triage and document API modules."""

from __future__ import annotations

import logging

logger = logging.getLogger(__name__)


def rearm_batch_barriers(batch_id: int, db) -> None:
    """Re-arm the batch's one-shot CAS flags before a re-run of EXTRACT.

    metadata_phase_queued_at (OCR→chat barrier) and analysis_queued_at (batch
    analysis) are set once and never reset on their own, so a re-extract of a
    single doc would otherwise find them already set: the barrier would refuse
    to dispatch METADATA and the analysis claim would refuse to dispatch
    ENRICH, leaving both to the (minutes-late) recovery sweep.
    """
    from sqlalchemy import text

    db.execute(
        text(
            "UPDATE ingest_batches SET metadata_phase_queued_at = NULL, "
            "analysis_queued_at = NULL WHERE id = :batch_id"
        ),
        {"batch_id": batch_id},
    )
    db.commit()
    logger.info(
        "Re-armed batch %d barriers (metadata_phase_queued_at, analysis_queued_at) "
        "for EXTRACT retry",
        batch_id,
    )


def dispatch_pipeline_retry(doc_id: int, batch_id: int | None, stage, db) -> None:
    """Dispatch (or re-dispatch) a stage's retry task, claiming it first.

    Which claim primitive to use depends on the stage's dispatch convention
    (see StageSpec.self_claims in pipeline_status.py):

    - Self-claiming stages (METADATA) must be dispatched UNCLAIMED — the
      task flips PENDING→RUNNING itself on entry. Pre-claiming here would
      leave the stage RUNNING and the dispatched task would then see RUNNING
      and skip as "already_claimed" — a permanent deadlock.
    - Batch-shared stages (BATCH_ANALYSIS) use the batch-level
      claim_batch_for_analysis CAS, not a per-doc claim — multiple docs in
      the same retry wave can independently compute BATCH_ANALYSIS as their
      head stage, so a per-doc claim wouldn't prevent duplicate dispatch.
    - EMBEDDINGS gets its own check: generate_embedding_task defers unclaimed
      (leaving the stage PENDING) when METADATA isn't yet terminal, so
      pre-claiming it in that situation would leave it stuck RUNNING and
      break metadata_task's later re-claim. But pre-claiming it when
      METADATA already IS terminal is fine — the task won't defer. So: if
      METADATA isn't terminal, don't dispatch EMBEDDINGS at all here;
      metadata_task's own cascade will claim and dispatch it once METADATA
      completes. This is a stage-*state* check, not just a stage-identity
      one, so it isn't expressed via StageSpec.self_claims.
    - Every other stage (EXTRACT, ENRICH, RELATIONSHIPS, CLAIMS, ENTITIES)
      mark_starts unconditionally and relies on the dispatcher's pre-claim
      for dedup, via the same claim_stage_for_dispatch primitive every
      cascade dispatcher already uses.
    """
    from app.models.enums import PipelineStage, StageStatus
    from app.services.pipeline_status import (
        STAGE_REGISTRY,
        claim_stage_for_dispatch,
        stages_dict,
    )
    from app.tasks.dispatch import dispatch_task

    spec = STAGE_REGISTRY[stage]
    arg = batch_id if spec.dispatch_arg == "batch_id" else doc_id
    if arg is None:
        logger.warning(
            "Cannot dispatch retry for %s — no %s available", stage, spec.dispatch_arg
        )
        return

    stage_label = stage.value if hasattr(stage, "value") else stage

    if stage == PipelineStage.EMBEDDINGS:
        from app.models.database import Document

        doc = db.query(Document).filter(Document.id == doc_id).first()
        metadata_status = (
            stages_dict(doc).get("metadata", {}).get("status") if doc else None
        )
        if metadata_status not in (
            StageStatus.COMPLETED.value,
            StageStatus.FAILED.value,
            StageStatus.SKIPPED.value,
        ):
            logger.info(
                "dispatch_pipeline_retry: doc %d METADATA not yet terminal "
                "(status=%s) — not dispatching EMBEDDINGS, metadata_task's "
                "own cascade will claim it once METADATA completes",
                doc_id,
                metadata_status,
            )
            return
        if not claim_stage_for_dispatch(doc_id, stage, db):
            logger.info(
                "dispatch_pipeline_retry: %s already claimed for doc %d — skipping",
                stage_label,
                doc_id,
            )
            return
    elif spec.dispatch_arg == "batch_id":
        from app.services.intelligence.orchestrator import claim_batch_for_analysis

        if not claim_batch_for_analysis(arg, db):
            logger.info(
                "dispatch_pipeline_retry: batch %s analysis already claimed — skipping",
                arg,
            )
            return
    elif not spec.self_claims:
        if not claim_stage_for_dispatch(doc_id, stage, db):
            logger.info(
                "dispatch_pipeline_retry: %s already claimed for doc %d — skipping",
                stage_label,
                doc_id,
            )
            return

    # Diagnostic: log every dispatch attempt so we can correlate which docs
    # actually got their tasks queued vs. which got lost in transit. When
    # recover_pipeline_task picks up "stuck dispatches" later, the missing
    # log lines here pinpoint exactly which dispatches never made it.
    logger.info(
        "dispatch_pipeline_retry: doc_id=%d batch_id=%s stage=%s task=%s",
        doc_id,
        batch_id,
        stage_label,
        spec.retry_task,
    )
    dispatch_task(spec.retry_task, arg)


def reset_batch_for_retry(batch, db, *, full: bool = False):
    """Reset pipeline stages for one batch without committing or dispatching.

    Returns (dispatch_items, batch_fallback) on success, or -1 if any stage is
    actively running (caller treats as skip/409). Does NOT commit — the caller
    must commit before calling dispatch_batch_retry with the returned items.

    dispatch_items is a list of (doc_id, batch_id, head_stage | None).
    batch_fallback is True when no per-doc head was found (BATCH_ANALYSIS fallback).

    Does not dispatch EMBEDDINGS itself: METADATA is unconditionally reset to
    PENDING above for every doc (no per-stage skip exemption applies to it),
    so it's never terminal at this point — dispatch_pipeline_retry's own
    METADATA-terminal gate would always no-op an EMBEDDINGS dispatch made
    here anyway. metadata_task's own claim_stage_for_dispatch cascade
    dispatches EMBEDDINGS once METADATA completes, same as any other
    non-retry completion.
    """
    from sqlalchemy import text as _text

    from app.models.database import Case, Proceeding
    from app.models.enums import (
        DocumentRole,
        IngestBatchStatus,
        PipelineStage,
        StageStatus,
    )
    from app.services.intelligence.batch_analyzer import _has_manual_groups
    from app.services.pipeline_status import (
        _STAGE_ORDER,
        clear_extraction_stamp,
        compute_overall_state,
        stages_dict,
    )

    # Re-read after any rollback so ORM state reflects DB reality.
    db.refresh(batch)

    # Bail out if any doc has a running OR retrying stage — a RETRYING stage
    # still has a live scheduled countdown; resetting it here would race the
    # countdown's own eventual redispatch. A lost RETRYING row (the countdown
    # never fires) is reclaimed by the PR3a orphan sweep, so this is safe to
    # block on rather than needing its own recovery path here.
    _IN_FLIGHT = {StageStatus.RUNNING.value, StageStatus.RETRYING.value}
    for doc in batch.documents:
        stages = stages_dict(doc)
        if any(
            v.get("status") in _IN_FLIGHT
            for v in stages.values()
            if isinstance(v, dict)
        ):
            return -1

    # Clear the cascade gate so the batch analysis can re-run
    batch.analysis_queued_at = None
    batch.status = IngestBatchStatus.PENDING
    if full:
        # metadata_phase_queued_at is a one-time CAS flag (see
        # claim_batch_for_metadata_phase) — once set it never resets on its
        # own. A full retry resets EXTRACT back to PENDING for every doc, so
        # once they all complete EXTRACT again, claim_batch_for_metadata_phase
        # would find the flag already set from the original run and refuse
        # to fire, permanently stalling the whole batch's METADATA phase.
        # Only `full` needs this: a partial retry leaves EXTRACT completed,
        # so the phase already correctly fired once and must not fire again.
        batch.metadata_phase_queued_at = None
    if batch.meta:
        meta = dict(batch.meta)
        meta.pop("reload_fired", None)
        batch.meta = meta

    # Clear the case-brief claim slot too — without this, the readiness
    # predicate in claim_case_brief_for_dispatch will eventually be re-
    # satisfied (after all docs in the retry finish CLAIMS) but the WHERE
    # brief_queued_at IS NULL guard would still fail, so no new brief would
    # ever fire for this wave. Cases affected by this batch's docs get the
    # reset; usually just one case per batch.
    affected_case_ids = {
        doc.case_id
        for doc in batch.documents
        if doc.case_id and doc.case_id != "_TRIAGE"
    }
    for case_id in affected_case_ids:
        c = db.query(Case).filter(Case.id == case_id).first()
        if c is not None:
            c.brief_queued_at = None

    dispatch_items: list = []

    # A user-organised bundle (triage sub-groups, or one built by slicing) is
    # the user's structure and batch analysis deliberately won't re-derive it,
    # so keep the roles and parent links instead of collapsing them.
    keep_structure = _has_manual_groups(batch.id, db)

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

        if full:
            clear_extraction_stamp(doc)
            # Re-analysing starts the bundle over: it is back in the inbox and
            # needs confirming again.
            doc.confirmed_at = None

        if not keep_structure:
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
                "proceeding_id = :proc_id, confirmed_at = :confirmed_at "
                "WHERE id = :doc_id"
            ),
            {
                "state": new_state.value,
                "case_id": doc.case_id,
                "proc_id": doc.proceeding_id,
                "confirmed_at": doc.confirmed_at,
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

        dispatch_items.append((doc.id, batch.id, head))

    batch_fallback = not any(head is not None for _, _, head in dispatch_items)
    return dispatch_items, batch_fallback


def dispatch_batch_retry(
    dispatch_items: list, *, batch_fallback: bool, batch_id: int, db
) -> None:
    """Dispatch Celery tasks from a plan built by reset_batch_for_retry.

    Must only be called after the DB transaction for the reset has committed.
    The actual task dispatch (dispatch_task) is fire-and-forget. The claim
    calls in front of it (claim_stage_for_dispatch, claim_batch_for_analysis)
    are plain DB calls, not wrapped here — an OperationalError from one of
    those does propagate out of this function.
    """
    for doc_id, b_id, head in dispatch_items:
        if head is not None:
            dispatch_pipeline_retry(doc_id, b_id, head, db)

    # Fallback: all per-doc cascade stages are already done but BATCH_ANALYSIS is still
    # pending — e.g. a batch-level retry after docs finished. The cascade won't fire it
    # naturally since no per-doc head task was dispatched.
    if batch_fallback:
        from app.services.intelligence.orchestrator import claim_batch_for_analysis

        if claim_batch_for_analysis(batch_id, db):
            from app.tasks.analyze_batch import analyze_batch_task
            from app.tasks.dispatch import dispatch_task

            dispatch_task(analyze_batch_task, batch_id)
