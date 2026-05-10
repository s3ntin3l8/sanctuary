"""Atomic per-stage pipeline-status tracker.

Each mutation issues a single SQL UPDATE using SQLite's json_set so that
concurrent Celery workers writing different stages of the same document
never race each other via Python read-modify-write.

Stage DAG lives in STAGE_REGISTRY — the single source of truth for ordering,
downstream cascades, and retry-task dispatch. _STAGE_ORDER and _DOWNSTREAM
are derived from it so they stay in sync automatically.
"""

import json
import logging
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from typing import Literal

from sqlalchemy import text
from sqlalchemy.orm import Session

from app.models.enums import PipelineStage, PipelineState, StageStatus

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Stage registry — single source of truth for the pipeline DAG.
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class StageSpec:
    stage: PipelineStage
    order: int  # lower = earlier; used for upstream-blocking checks
    downstream: tuple[PipelineStage, ...] = field(default_factory=tuple)
    retry_task: str = ""  # dotted Celery task name
    dispatch_arg: Literal["doc_id", "batch_id"] = "doc_id"


STAGE_REGISTRY: dict[PipelineStage, StageSpec] = {
    PipelineStage.EXTRACT: StageSpec(
        stage=PipelineStage.EXTRACT,
        order=0,
        downstream=(
            PipelineStage.METADATA,
            PipelineStage.PROCEEDING_ANALYSIS,
            PipelineStage.ENRICH,
            PipelineStage.RELATIONSHIPS,
            PipelineStage.CLAIMS,
            PipelineStage.ENTITIES,
        ),
        retry_task="app.tasks.document_processing.process_document_task",
    ),
    PipelineStage.METADATA: StageSpec(
        stage=PipelineStage.METADATA,
        order=1,
        downstream=(
            PipelineStage.PROCEEDING_ANALYSIS,
            PipelineStage.ENRICH,
            PipelineStage.RELATIONSHIPS,
            PipelineStage.CLAIMS,
            PipelineStage.ENTITIES,
        ),
        retry_task="app.tasks.document_processing.process_document_task",
    ),
    PipelineStage.PROCEEDING_ANALYSIS: StageSpec(
        stage=PipelineStage.PROCEEDING_ANALYSIS,
        order=2,
        downstream=(
            PipelineStage.BATCH_ANALYSIS,
            PipelineStage.ENRICH,
            PipelineStage.RELATIONSHIPS,
            PipelineStage.CLAIMS,
            PipelineStage.ENTITIES,
        ),
        retry_task="app.tasks.analyze_proceeding.analyze_proceeding_task",
    ),
    PipelineStage.BATCH_ANALYSIS: StageSpec(
        stage=PipelineStage.BATCH_ANALYSIS,
        order=3,
        downstream=(),
        retry_task="app.tasks.analyze_batch.analyze_batch_task",
        dispatch_arg="batch_id",
    ),
    PipelineStage.ENRICH: StageSpec(
        stage=PipelineStage.ENRICH,
        order=4,
        downstream=(
            PipelineStage.RELATIONSHIPS,
            PipelineStage.CLAIMS,
            PipelineStage.ENTITIES,
        ),
        retry_task="app.tasks.enrich_document.enrich_document_task",
    ),
    PipelineStage.RELATIONSHIPS: StageSpec(
        stage=PipelineStage.RELATIONSHIPS,
        order=5,
        downstream=(),
        retry_task="app.tasks.detect_relationships.detect_relationships_task",
    ),
    PipelineStage.CLAIMS: StageSpec(
        stage=PipelineStage.CLAIMS,
        order=6,
        downstream=(),
        retry_task="app.tasks.extract_claims.extract_claims_task",
    ),
    PipelineStage.ENTITIES: StageSpec(
        stage=PipelineStage.ENTITIES,
        order=7,
        downstream=(),
        retry_task="app.tasks.extract_entities.extract_entities_task",
    ),
    PipelineStage.EMBEDDINGS: StageSpec(
        stage=PipelineStage.EMBEDDINGS,
        order=8,
        downstream=(),
        retry_task="app.tasks.generate_embedding.generate_embedding_task",
    ),
}

# Guard: every PipelineStage member must have a registry entry.
_missing = set(PipelineStage) - set(STAGE_REGISTRY)
if _missing:
    raise RuntimeError(
        f"STAGE_REGISTRY is missing entries for: {_missing}. "
        "Add a StageSpec for each new PipelineStage member."
    )

# Derived structures — kept for backward compat with any code that imports them directly.
_STAGE_ORDER: list[StageSpec] = sorted(STAGE_REGISTRY.values(), key=lambda s: s.order)
_DOWNSTREAM: dict[PipelineStage, list[PipelineStage]] = {
    s.stage: list(s.downstream) for s in STAGE_REGISTRY.values()
}


# ---------------------------------------------------------------------------
# Public helpers
# ---------------------------------------------------------------------------


def _now_iso() -> str:
    return datetime.now(UTC).isoformat()


def initialize(doc, batched: bool) -> None:
    """Set all stages to pending on a newly created Document (before first commit).

    Call this right before the session commit that creates the doc row so that
    pipeline_stages / pipeline_state are persisted together with the new row.
    No SQL UPDATE is needed — we're within the same SQLAlchemy session.
    """
    stages: dict[str, dict] = {}
    for stage in PipelineStage:
        if stage == PipelineStage.BATCH_ANALYSIS and not batched:
            stages[stage.value] = {
                "status": StageStatus.SKIPPED.value,
                "reason": "no batch (manual upload)",
            }
        else:
            stages[stage.value] = {"status": StageStatus.PENDING.value}
    doc.pipeline_stages = stages
    doc.pipeline_state = PipelineState.PENDING


def mark_started(doc_id: int, stage: PipelineStage, db: Session) -> None:
    # Clear any retry bookkeeping from a prior RETRYING record so the next
    # attempt presents as a clean RUNNING state.
    _update_stage(
        doc_id,
        stage,
        db,
        status=StageStatus.RUNNING,
        extra_sets={
            "started_at": _now_iso(),
            "attempt": None,
            "max_attempts": None,
            "next_at": None,
        },
        commit=True,  # early commit so the UI flips to RUNNING immediately
    )


def claim_stage_for_dispatch(doc_id: int, stage: PipelineStage, db: Session) -> bool:
    """Atomically transition a stage from pending→running; return True if claimed.

    Prevents fan-out when two concurrent callers both observe a stage as pending.
    Only one caller wins the conditional UPDATE; the other sees rowcount=0 and
    skips dispatch. The winning caller should then dispatch the Celery task.

    The task itself calls mark_started() on entry, which stamps started_at and
    re-commits. If the dispatch succeeds but the task is lost (worker crash),
    recover_orphaned_running_stages() handles the stale running state.
    """
    sk = stage.value
    result = db.execute(
        text(
            f"UPDATE documents "
            f"SET pipeline_stages = json_set(COALESCE(pipeline_stages, '{{}}'), '$.{sk}.status', :running) "
            f"WHERE id = :doc_id "
            f"AND json_extract(COALESCE(pipeline_stages, '{{}}'), '$.{sk}.status') = :pending"
        ),
        {
            "doc_id": doc_id,
            "running": StageStatus.RUNNING.value,
            "pending": StageStatus.PENDING.value,
        },
    )
    if result.rowcount == 0:
        db.commit()
        return False

    # Recompute pipeline_state so the UI transitions to RUNNING immediately.
    row = db.execute(
        text("SELECT pipeline_stages FROM documents WHERE id = :doc_id"),
        {"doc_id": doc_id},
    ).fetchone()
    stages: dict = json.loads(row[0]) if (row and row[0]) else {}
    overall = compute_overall_state(stages)
    db.execute(
        text("UPDATE documents SET pipeline_state = :state WHERE id = :doc_id"),
        {"state": overall.value, "doc_id": doc_id},
    )
    db.commit()
    return True


def mark_completed(
    doc_id: int, stage: PipelineStage, db: Session, *, commit: bool = True
) -> None:
    _update_stage(
        doc_id,
        stage,
        db,
        status=StageStatus.COMPLETED,
        extra_sets={"completed_at": _now_iso(), "error": None},
        commit=commit,
    )


def mark_failed(
    doc_id: int,
    stage: PipelineStage,
    db: Session,
    error: str = "",
    *,
    commit: bool = True,
) -> None:
    _update_stage(
        doc_id,
        stage,
        db,
        status=StageStatus.FAILED,
        extra_sets={
            "completed_at": _now_iso(),
            "error": error,
            "attempt": None,
            "max_attempts": None,
            "next_at": None,
        },
        commit=commit,
    )


def mark_retrying(
    doc_id: int,
    stage: PipelineStage,
    db: Session,
    *,
    error: str,
    attempt: int,
    max_attempts: int,
    next_at: str,
    commit: bool = True,
) -> None:
    """Mark a stage as awaiting retry — its last attempt failed and another is scheduled.

    Treated as in-flight by `compute_overall_state` (rolls up to PipelineState.RUNNING)
    so polling templates keep refreshing. `next_at` is an ISO timestamp the UI can
    render as a countdown; `attempt`/`max_attempts` provide "2/3" context.
    """
    _update_stage(
        doc_id,
        stage,
        db,
        status=StageStatus.RETRYING,
        extra_sets={
            "error": error,
            "attempt": attempt,
            "max_attempts": max_attempts,
            "next_at": next_at,
        },
        commit=commit,
    )


def schedule_retry(
    doc_id: int,
    stage: PipelineStage,
    db: Session,
    *,
    error: str,
    attempt: int,
    max_attempts: int,
    countdown: int,
) -> None:
    """Convenience wrapper: compute next_at from a countdown and call mark_retrying.

    Used at every Celery `self.retry(...)` site — keeps the call shape uniform
    so the UI sees a consistent "Retrying STAGE (attempt/max) in Ns" record.
    """
    from datetime import timedelta

    next_at = (datetime.now(UTC) + timedelta(seconds=countdown)).isoformat()
    mark_retrying(
        doc_id,
        stage,
        db,
        error=error,
        attempt=attempt,
        max_attempts=max_attempts,
        next_at=next_at,
    )


def mark_failed_with_cascade(
    doc_id: int,
    stage: PipelineStage,
    db: Session,
    error: str = "",
) -> None:
    """Mark `stage` failed and propagate failure to its per-doc downstream stages.

    Used when a stage's failure means downstream per-doc work cannot run
    (e.g. EXTRACT fails → METADATA, ENRICH, … cannot proceed).
    The cascade is not sticky — a successful retry naturally overwrites cascaded
    failed states as each stage runs and calls mark_started/mark_completed.
    """
    mark_failed(doc_id, stage, db, error=error, commit=False)
    for downstream in _DOWNSTREAM.get(stage, []):
        _update_stage(
            doc_id,
            downstream,
            db,
            status=StageStatus.FAILED,
            extra_sets={
                "completed_at": _now_iso(),
                "error": f"upstream {stage.value} failed",
            },
            commit=False,
        )
    db.commit()


def mark_skipped(
    doc_id: int,
    stage: PipelineStage,
    db: Session,
    reason: str = "",
    *,
    commit: bool = True,
) -> None:
    _update_stage(
        doc_id,
        stage,
        db,
        status=StageStatus.SKIPPED,
        extra_sets={"reason": reason},
        commit=commit,
    )


def reset_stage(doc_id: int, stage: PipelineStage, db: Session) -> None:
    """Reset a stage (and its downstream dependents) to PENDING for retry."""
    stages_to_reset = [stage] + _DOWNSTREAM.get(stage, [])
    for s in stages_to_reset:
        _update_stage(
            doc_id,
            s,
            db,
            status=StageStatus.PENDING,
            extra_sets={
                "started_at": None,
                "completed_at": None,
                "error": None,
                "reason": None,
                "attempt": None,
                "max_attempts": None,
                "next_at": None,
            },
            commit=False,
        )
    db.commit()


def reset_all_stages(doc_id: int, db: Session) -> None:
    """Reset every non-skipped stage to PENDING.

    Used by the document HUD's "retry all" action. SKIPPED stages stay skipped
    (e.g. BATCH_ANALYSIS on manually-uploaded docs); everything else is cleared
    of error/timestamps so the pipeline can run again from EXTRACT.
    """
    row = db.execute(
        text("SELECT pipeline_stages FROM documents WHERE id = :doc_id"),
        {"doc_id": doc_id},
    ).fetchone()
    if row is None:
        return
    stages: dict = json.loads(row[0]) if row[0] else {}
    for stage_key, record in stages.items():
        try:
            stage_enum = PipelineStage(stage_key)
        except ValueError:
            continue

        # Preserve "permanent" skips that shouldn't be re-evaluated
        if record.get("status") == StageStatus.SKIPPED.value and record.get(
            "reason"
        ) in (
            "manual upload",
            "no batch (manual upload)",
        ):
            continue

        _update_stage(
            doc_id,
            stage_enum,
            db,
            status=StageStatus.PENDING,
            extra_sets={
                "started_at": None,
                "completed_at": None,
                "error": None,
                "attempt": None,
                "max_attempts": None,
                "next_at": None,
            },
            commit=False,
        )
    db.commit()


def compute_overall_state(stages: dict) -> PipelineState:
    """Derive overall PipelineState from per-stage dict.

    RETRYING is in-flight (last attempt failed, next is queued) — rolls up to
    RUNNING so polling templates keep refreshing through the retry window.
    """
    if not stages:
        return PipelineState.PENDING
    statuses = {v.get("status") for v in stages.values()}
    if StageStatus.RUNNING.value in statuses or StageStatus.RETRYING.value in statuses:
        return PipelineState.RUNNING
    if StageStatus.FAILED.value in statuses:
        return PipelineState.FAILED
    terminal = {StageStatus.COMPLETED.value, StageStatus.SKIPPED.value}
    if statuses <= terminal:
        return PipelineState.COMPLETED
    if StageStatus.PENDING.value in statuses and statuses & terminal:
        return PipelineState.PARTIAL
    return PipelineState.PENDING


def get_upstream_blocking(stage: PipelineStage, stages: dict) -> list[str]:
    """Return stage names that are currently RUNNING upstream of `stage`.

    Used by the retry endpoint to reject 409 when an upstream stage is active.
    """
    spec = STAGE_REGISTRY[stage]
    upstream = [s for s in _STAGE_ORDER if s.order < spec.order]
    blocking = []
    for s in upstream:
        record = stages.get(s.stage.value, {})
        if record.get("status") == StageStatus.RUNNING.value:
            blocking.append(s.stage.value)
    return blocking


def aggregate_pipeline_summary(stages_per_doc: list[dict]) -> dict:
    """Compute aggregate stage-status counts across all docs in a bundle.

    Accepts a list of pipeline_stages dicts (one per document). Returns the
    same shape as BundleView.pipeline_summary so callers are interchangeable.
    """
    from collections import Counter

    counts: Counter = Counter()
    for stages in stages_per_doc:
        state = compute_overall_state(stages)
        counts[state.value] += 1
    return {"total": len(stages_per_doc), **counts}


def recover_orphaned_running_stages(db: Session) -> dict:
    """Reset any pipeline stages left in RUNNING/RETRYING state due to a prior crash.

    Called once at app startup after migrations. Finds documents with
    pipeline_state in (RUNNING, PARTIAL), resets every RUNNING or RETRYING stage
    back to PENDING (without cascading to downstream stages), recomputes
    pipeline_state, and unblocks affected IngestBatches. RETRYING stages are
    stuck the same way as RUNNING ones — a worker that died between
    `mark_retrying` and the next Celery attempt leaves them in flight forever.

    Returns {"docs_reset": N, "stages_reset": N, "batches_reset": N}.
    """
    from app.models.database import Document, IngestBatch
    from app.models.enums import IngestBatchStatus

    docs = (
        db.query(Document)
        .filter(Document.pipeline_state.in_(["running", "partial"]))
        .all()
    )

    docs_reset = 0
    stages_reset = 0
    affected_batch_ids: set[int] = set()
    batch_analysis_reset_ids: set[int] = set()

    _IN_FLIGHT = {StageStatus.RUNNING.value, StageStatus.RETRYING.value}
    for doc in docs:
        stages: dict = doc.pipeline_stages or {}
        stuck = [
            key
            for key, val in stages.items()
            if isinstance(val, dict) and val.get("status") in _IN_FLIGHT
        ]
        if not stuck:
            continue

        for stage_key in stuck:
            try:
                stage_enum = PipelineStage(stage_key)
            except ValueError:
                continue

            # Reset only the stuck stage itself — do NOT cascade to already-completed
            # downstream stages. reset_stage() is for user-initiated retries where
            # re-running downstream is intentional; here we're just clearing an
            # in-flight lock left by a crash.
            _update_stage(
                doc.id,
                stage_enum,
                db,
                status=StageStatus.PENDING,
                extra_sets={
                    "started_at": None,
                    "completed_at": None,
                    "error": None,
                    "attempt": None,
                    "max_attempts": None,
                    "next_at": None,
                },
                commit=False,
            )
            stages_reset += 1
            if stage_enum == PipelineStage.BATCH_ANALYSIS and doc.ingest_batch_id:
                batch_analysis_reset_ids.add(doc.ingest_batch_id)

        db.refresh(doc)
        doc.pipeline_state = compute_overall_state(doc.pipeline_stages or {})

        docs_reset += 1
        if doc.ingest_batch_id:
            affected_batch_ids.add(doc.ingest_batch_id)

    for batch_id in affected_batch_ids:
        batch = db.query(IngestBatch).filter(IngestBatch.id == batch_id).first()
        if not batch:
            continue
        # Only unblock the batch claim when BATCH_ANALYSIS itself was stuck —
        # clearing it when only PROCEEDING_ANALYSIS was stuck triggers redundant
        # re-analysis of an already-completed batch.
        if batch_id in batch_analysis_reset_ids:
            batch.analysis_queued_at = None
        if batch.status == IngestBatchStatus.PROCESSING:
            batch.status = IngestBatchStatus.PENDING

    if docs_reset:
        db.commit()

    return {
        "docs_reset": docs_reset,
        "stages_reset": stages_reset,
        "batches_reset": len(affected_batch_ids),
    }


def recover_stuck_batches(db: Session, *, max_age_seconds: int = 3600) -> dict:
    """Find batches where analysis_queued_at is set but analysis never completed.

    Finds batches stuck > 1 hour with analysis_queued_at set, checks if any docs
    are still running, and if not, clears the claim to allow re-triggering.
    """

    from app.models.database import Document, IngestBatch
    from app.models.enums import IngestBatchStatus, PipelineState

    cutoff = datetime.now(UTC) - timedelta(seconds=max_age_seconds)

    # Find batches stuck with analysis_queued_at set before cutoff
    stuck_batches = (
        db.query(IngestBatch)
        .filter(
            IngestBatch.analysis_queued_at.isnot(None),
            IngestBatch.analysis_queued_at < cutoff,
        )
        .all()
    )

    recovered_ids = []
    for batch in stuck_batches:
        # Check if any docs in this batch are still in RUNNING state
        running_docs = (
            db.query(Document)
            .filter(
                Document.ingest_batch_id == batch.id,
                Document.pipeline_state == PipelineState.RUNNING.value,
            )
            .count()
        )

        if running_docs == 0:
            # No docs are running, but batch is still "claimed". Release it.
            batch.analysis_queued_at = None
            if batch.status == IngestBatchStatus.PROCESSING:
                batch.status = IngestBatchStatus.PENDING
            recovered_ids.append(batch.id)

    if recovered_ids:
        db.commit()
        logger.info("recover_stuck_batches: released %d batch(es)", len(recovered_ids))

    return {"batches_recovered": len(recovered_ids), "batch_ids": recovered_ids}


def recover_stuck_pending_dispatches(db: Session, *, max_age_seconds: int = 60) -> dict:
    """Re-dispatch docs whose pipeline got stalled mid-cascade.

    Sibling to recover_orphaned_running_stages. Catches the EAGER+uvicorn-reload
    hazard: a stage's dispatch (or the chain of dispatches) got killed by
    uvicorn --reload. The doc is left with pipeline_state in (PENDING, PARTIAL)
    and one or more pending stages, but no stage is currently RUNNING (those
    are handled by the running-state recovery first).

    Resume strategy: find the first pending stage in cascade order (the head),
    look up its retry_task in STAGE_REGISTRY, and dispatch it. The retry_task
    is idempotent over already-completed upstream stages.

    A doc qualifies when:
      * pipeline_state in (PENDING, PARTIAL)
      * ingest_date < now - max_age_seconds (skip just-uploaded docs that
        haven't had a chance to run yet)
      * No stage is currently RUNNING (running-state recovery owns those)
      * There IS a pending head stage that can resume the cascade

    Returns {"docs_redispatched": N, "doc_ids": [...]}.
    """
    import importlib
    from datetime import timedelta

    from app.models.database import Document

    cutoff = datetime.now(UTC).replace(tzinfo=None) - timedelta(seconds=max_age_seconds)

    candidates = (
        db.query(Document)
        .filter(
            Document.pipeline_state.in_(
                [PipelineState.PENDING.value, PipelineState.PARTIAL.value]
            ),
            Document.ingest_date < cutoff,
        )
        .all()
    )

    # BATCH_ANALYSIS is batch-level (dispatch_arg="batch_id") and re-driven by
    # the bundle retry endpoint, not per-doc. Skip it here.
    skip_stages = {PipelineStage.BATCH_ANALYSIS}

    redispatched: list[int] = []
    for doc in candidates:
        stages: dict = doc.pipeline_stages or {}

        # Skip if any stage is currently running — running-state recovery owns it.
        if any(
            isinstance(v, dict) and v.get("status") == StageStatus.RUNNING.value
            for v in stages.values()
        ):
            continue

        # Find the head pending stage in cascade order.
        head_spec = None
        for spec in _STAGE_ORDER:
            if spec.stage in skip_stages:
                continue
            stage_record = stages.get(spec.stage.value, {})
            if not isinstance(stage_record, dict):
                continue
            status = stage_record.get("status")
            if status == StageStatus.PENDING.value:
                head_spec = spec
                break
            if status not in (
                StageStatus.COMPLETED.value,
                StageStatus.SKIPPED.value,
            ):
                # Non-terminal upstream blocker (failed) — let it be.
                break

        if head_spec is None or head_spec.dispatch_arg != "doc_id":
            continue

        # Lazy imports — pipeline_status is also imported during task execution.
        from app.tasks.dispatch import dispatch_task

        module_path, name = head_spec.retry_task.rsplit(".", 1)
        try:
            task = getattr(importlib.import_module(module_path), name)
        except (ImportError, AttributeError):
            logger.exception(
                "Stuck-pending recovery: cannot resolve retry_task %s for doc %d",
                head_spec.retry_task,
                doc.id,
            )
            continue

        dispatch_task(task, doc.id)
        redispatched.append(doc.id)

    return {"docs_redispatched": len(redispatched), "doc_ids": redispatched}


# ---------------------------------------------------------------------------
# Internal helpers
# ---------------------------------------------------------------------------


def _update_stage(
    doc_id: int,
    stage: PipelineStage,
    db: Session,
    status: StageStatus,
    extra_sets: dict,
    *,
    commit: bool = True,
) -> None:
    """Update a single stage key inside pipeline_stages and recompute pipeline_state.

    Uses json_set / json_remove so each UPDATE touches only the named keys,
    never overwriting sibling stages written by concurrent threads (SQLite
    serialises writes so the RHS json_set reads the last committed state).
    """
    sk = stage.value  # e.g. "enrich"

    # Build json_set argument list: path, value, path, value, ...
    set_pairs: list[str] = [f"'$.{sk}.status'", ":_status_val"]
    params: dict = {"_status_val": status.value, "doc_id": doc_id}

    remove_paths: list[str] = []
    for key, val in extra_sets.items():
        if val is None:
            remove_paths.append(f"'$.{sk}.{key}'")
        else:
            pname = f"_extra_{key}"
            set_pairs.extend([f"'$.{sk}.{key}'", f":{pname}"])
            params[pname] = val

    json_expr = f"json_set(COALESCE(pipeline_stages, '{{}}'), {', '.join(set_pairs)})"
    if remove_paths:
        json_expr = f"json_remove({json_expr}, {', '.join(remove_paths)})"

    result = db.execute(
        text(f"UPDATE documents SET pipeline_stages = {json_expr} WHERE id = :doc_id"),
        params,
    )
    if result.rowcount == 0:
        logger.warning("pipeline_status: doc %d not found", doc_id)
        return

    # Recompute pipeline_state from the freshly-written stages.
    row = db.execute(
        text("SELECT pipeline_stages FROM documents WHERE id = :doc_id"),
        {"doc_id": doc_id},
    ).fetchone()
    stages: dict = json.loads(row[0]) if (row and row[0]) else {}
    overall = compute_overall_state(stages)
    db.execute(
        text("UPDATE documents SET pipeline_state = :state WHERE id = :doc_id"),
        {"state": overall.value, "doc_id": doc_id},
    )
    if commit:
        db.commit()
