"""Atomic per-stage pipeline-status tracker.

Each mutation writes a single row in document_pipeline_stages so that
concurrent Celery workers writing different stages of the same document
never race each other via Python read-modify-write.

Stage DAG lives in STAGE_REGISTRY — the single source of truth for ordering,
downstream cascades, and retry-task dispatch. _STAGE_ORDER and _DOWNSTREAM
are derived from it so they stay in sync automatically.
"""

import logging
import time
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from typing import Literal

from sqlalchemy import text
from sqlalchemy.exc import OperationalError
from sqlalchemy.orm import Session

from app.core.timezone import naive_utc_now, to_naive
from app.models.enums import PipelineStage, PipelineState, StageStatus

logger = logging.getLogger(__name__)

# SQL injection hardening: whitelist of allowed extra_sets keys in _update_stage()
_ALLOWED_EXTRA_KEYS = frozenset(
    {
        "started_at",
        "completed_at",
        "error",
        "reason",
        "attempt",
        "max_attempts",
        "next_at",
    }
)

# ---------------------------------------------------------------------------
# Stage registry — single source of truth for the pipeline DAG.
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class StageSpec:
    """Static metadata for a pipeline stage.

    `order` is purely cosmetic — used only by the UI to sort stages left-to-
    right in the queue panel and bundle status displays. It does NOT encode
    dependency semantics; two stages can run in parallel and still have
    different orders.

    `depends_on` is the dependency DAG — the direct upstream stages that must
    finish before this stage's task can run. Used by get_upstream_blocking to
    compute "what's blocking a retry of this stage?". Transitively traversed.

    `downstream` is the cascade-failure list — when this stage fails, every
    stage listed here is marked failed too (because they can't proceed
    without this stage's output). Used by mark_failed_with_cascade and
    reset_stage. Typically the transitive closure of depends_on inverted,
    though not enforced.
    """

    stage: PipelineStage
    order: int  # display position only — see class docstring
    label: str = ""  # human-readable label for UI (e.g. "Batch Analysis")
    icon: str = ""  # Material Symbols icon name for the stepper dot
    depends_on: tuple[PipelineStage, ...] = field(default_factory=tuple)
    downstream: tuple[PipelineStage, ...] = field(default_factory=tuple)
    retry_task: str = ""  # dotted Celery task name
    dispatch_arg: Literal["doc_id", "batch_id"] = "doc_id"


# Real dispatch dependencies (from the per-task .delay() chains):
#   process_document_task: EXTRACT → METADATA, then fans out to BATCH_ANALYSIS
#     (gated on all batch siblings' METADATA done) AND EMBEDDINGS (parallel).
#   analyze_batch_task: BATCH_ANALYSIS → ENRICH per doc.
#   enrich_document_task: ENRICH → RELATIONSHIPS, ENTITIES (parallel siblings).
#   detect_relationships_task: RELATIONSHIPS → CLAIMS.
#
# Display order (`order` field) reflects when each stage finishes in wallclock
# time on the typical fast-path: EMBEDDINGS dispatches alongside BATCH_ANALYSIS
# but finishes much earlier, so it sits right after METADATA in the display.
STAGE_REGISTRY: dict[PipelineStage, StageSpec] = {
    PipelineStage.EXTRACT: StageSpec(
        stage=PipelineStage.EXTRACT,
        order=0,
        label="Extract",
        icon="file_upload",
        depends_on=(),
        downstream=(
            PipelineStage.METADATA,
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
        label="Metadata",
        icon="description",
        depends_on=(PipelineStage.EXTRACT,),
        downstream=(
            PipelineStage.BATCH_ANALYSIS,
            PipelineStage.ENRICH,
            PipelineStage.RELATIONSHIPS,
            PipelineStage.CLAIMS,
            PipelineStage.ENTITIES,
        ),
        retry_task="app.tasks.document_processing.process_document_task",
    ),
    PipelineStage.EMBEDDINGS: StageSpec(
        stage=PipelineStage.EMBEDDINGS,
        order=2,
        label="Embeddings",
        icon="workspaces",
        depends_on=(PipelineStage.METADATA,),
        downstream=(),
        retry_task="app.tasks.generate_embedding.generate_embedding_task",
    ),
    PipelineStage.BATCH_ANALYSIS: StageSpec(
        stage=PipelineStage.BATCH_ANALYSIS,
        order=3,
        label="Batch Analysis",
        icon="batch_prediction",
        depends_on=(PipelineStage.METADATA,),
        downstream=(),
        retry_task="app.tasks.analyze_batch.analyze_batch_task",
        dispatch_arg="batch_id",
    ),
    PipelineStage.ENRICH: StageSpec(
        stage=PipelineStage.ENRICH,
        order=4,
        label="Enrich",
        icon="auto_fix_high",
        depends_on=(PipelineStage.BATCH_ANALYSIS,),
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
        label="Relationships",
        icon="account_tree",
        depends_on=(PipelineStage.ENRICH,),
        downstream=(),
        retry_task="app.tasks.detect_relationships.detect_relationships_task",
    ),
    PipelineStage.CLAIMS: StageSpec(
        stage=PipelineStage.CLAIMS,
        order=6,
        label="Claims",
        icon="format_list_bulleted",
        depends_on=(PipelineStage.RELATIONSHIPS,),
        downstream=(),
        retry_task="app.tasks.extract_claims.extract_claims_task",
    ),
    PipelineStage.ENTITIES: StageSpec(
        stage=PipelineStage.ENTITIES,
        order=7,
        label="Entities",
        icon="workspaces_outline",
        depends_on=(PipelineStage.ENRICH,),
        downstream=(),
        retry_task="app.tasks.extract_entities.extract_entities_task",
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


def stage_display_list() -> list[dict]:
    """Return all stages sorted by display order as dicts with the keys
    templates use — `key`, `icon`, `label`. The horizontal pipeline-stepper
    template reads this via the `pipeline_stages` Jinja global so the
    registry is the single source of truth for both order and labels."""
    return [
        {"key": spec.stage.value, "icon": spec.icon, "label": spec.label}
        for spec in _STAGE_ORDER
    ]


def _compute_transitive_upstream(target: PipelineStage) -> frozenset[PipelineStage]:
    """BFS through depends_on edges to collect every transitive ancestor of
    `target`. Used by get_upstream_blocking."""
    visited: set[PipelineStage] = set()
    queue: list[PipelineStage] = list(STAGE_REGISTRY[target].depends_on)
    while queue:
        s = queue.pop()
        if s in visited:
            continue
        visited.add(s)
        queue.extend(STAGE_REGISTRY[s].depends_on)
    return frozenset(visited)


# Transitive upstream set per stage, computed once at module load.
# get_upstream_blocking iterates this rather than re-traversing on every call.
_UPSTREAM: dict[PipelineStage, frozenset[PipelineStage]] = {
    s: _compute_transitive_upstream(s) for s in STAGE_REGISTRY
}


# ---------------------------------------------------------------------------
# Public helpers
# ---------------------------------------------------------------------------


def stages_dict(doc) -> dict:
    """Return pipeline stages as a dict keyed by stage name.

    Reads from doc.stage_rows (the document_pipeline_stages ORM relationship).
    Shape: {stage_name: {"status": ..., "started_at": ..., ...}} — only keys
    with non-None values are included, matching the old JSON column shape.
    """

    def _iso(dt):
        return dt.isoformat() if dt is not None else None

    return {
        row.stage: {
            k: v
            for k, v in {
                "status": row.status,
                "started_at": _iso(row.started_at),
                "completed_at": _iso(row.completed_at),
                "error": row.error,
                "reason": row.reason,
                "attempt": row.attempt,
                "max_attempts": row.max_attempts,
                "next_at": _iso(row.next_at),
            }.items()
            if v is not None
        }
        for row in (doc.stage_rows if doc is not None else [])
    }


def initialize(doc, batched: bool, db: Session) -> None:
    """Set all stages to pending. Call after db.add(doc) + db.flush() so doc.id exists."""
    from app.models.database import DocumentPipelineStage

    stage_rows = []
    for stage in PipelineStage:
        if stage == PipelineStage.BATCH_ANALYSIS and not batched:
            stage_rows.append(
                DocumentPipelineStage(
                    document_id=doc.id,
                    stage=stage.value,
                    status=StageStatus.SKIPPED.value,
                    reason="no batch (manual upload)",
                )
            )
        else:
            stage_rows.append(
                DocumentPipelineStage(
                    document_id=doc.id,
                    stage=stage.value,
                    status=StageStatus.PENDING.value,
                )
            )
    db.add_all(stage_rows)
    db.flush()
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
            "started_at": naive_utc_now(),
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
    assert sk.isidentifier(), f"pipeline_status: invalid stage key {sk!r}"
    result = db.execute(
        text(
            "UPDATE document_pipeline_stages SET status = :running "
            "WHERE document_id = :doc_id AND stage = :stage AND status = :pending"
        ),
        {
            "doc_id": doc_id,
            "stage": sk,
            "running": StageStatus.RUNNING.value,
            "pending": StageStatus.PENDING.value,
        },
    )
    if result.rowcount == 0:
        db.commit()
        return False

    from app.models.database import Document

    doc_instance = db.get(Document, doc_id)
    if doc_instance is not None:
        db.expire(doc_instance, ["stage_rows"])

    rows = db.execute(
        text(
            "SELECT stage, status FROM document_pipeline_stages WHERE document_id = :doc_id"
        ),
        {"doc_id": doc_id},
    ).fetchall()
    current = {row[0]: {"status": row[1]} for row in rows}
    overall = compute_overall_state(current)
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
        extra_sets={"completed_at": naive_utc_now(), "error": None},
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
            "completed_at": naive_utc_now(),
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
    next_at,
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
    next_at = to_naive(datetime.now(UTC) + timedelta(seconds=countdown))
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
                "completed_at": naive_utc_now(),
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
    rows = db.execute(
        text(
            "SELECT stage, status, reason FROM document_pipeline_stages WHERE document_id = :doc_id"
        ),
        {"doc_id": doc_id},
    ).fetchall()
    if not rows:
        return
    for stage_key, status_val, reason_val in rows:
        try:
            stage_enum = PipelineStage(stage_key)
        except ValueError:
            continue

        if status_val == StageStatus.SKIPPED.value and reason_val in (
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
                "reason": None,
            },
            commit=False,
        )
    db.commit()


def reset_failed_stages_only(doc_id: int, db: Session) -> None:
    """Reset only FAILED pipeline stages to PENDING.

    Preserves COMPLETED and SKIPPED stages so a retry resumes from the
    first failed stage rather than re-running the entire pipeline from EXTRACT.
    Used by the queue's retry-failed action.

    Cascade-failed stages (status=failed, error="upstream X failed") are also
    reset — they become PENDING and will re-run once their upstream succeeds.
    """
    rows = db.execute(
        text(
            "SELECT stage, status FROM document_pipeline_stages WHERE document_id = :doc_id"
        ),
        {"doc_id": doc_id},
    ).fetchall()
    if not rows:
        return
    for stage_key, status_val in rows:
        if status_val != StageStatus.FAILED.value:
            continue
        try:
            stage_enum = PipelineStage(stage_key)
        except ValueError:
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
                "reason": None,
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

    "Upstream" is computed from the dependency DAG (StageSpec.depends_on),
    not from `order` — parallel sibling stages with a lower display order do
    NOT count. Used by the retry endpoint to reject 409 when an actual
    prerequisite is still in flight.
    """
    upstream = _UPSTREAM.get(stage, frozenset())
    # Stable order for the error message: by ascending display order.
    sorted_upstream = sorted(upstream, key=lambda s: STAGE_REGISTRY[s].order)
    blocking = []
    for upstream_stage in sorted_upstream:
        record = stages.get(upstream_stage.value, {})
        if record.get("status") == StageStatus.RUNNING.value:
            blocking.append(upstream_stage.value)
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


def recover_orphaned_running_stages(
    db: Session, *, min_age_seconds: int = 1200
) -> dict:
    """Reset pipeline stages stuck in RUNNING/RETRYING state past their
    expected runtime — presumed orphaned by a crashed worker.

    Originally written as a startup-only one-shot (when any RUNNING stage
    IS a crash artifact, since the worker has just rebooted). Now also
    runs on a 5-min cron via recover_pipeline_task — at which point an
    unbounded reset will kill legitimately-running long tasks (the
    batch_analyzer routinely runs 4-5 minutes; the enricher 2-3 min).
    The `min_age_seconds` guard keeps the cron-mode safe: only stages
    whose started_at is older than the threshold count as orphaned.

    20 min default covers the slowest legitimate stage (batch_analyzer
    with retries) by ~3×; tighter recovery for genuinely-crashed stages
    still happens within one extra cron tick after the threshold passes.

    Returns {"docs_reset": N, "stages_reset": N, "batches_reset": N}.
    """
    from app.models.database import Document, IngestBatch
    from app.models.enums import IngestBatchStatus

    cutoff = naive_utc_now() - timedelta(seconds=min_age_seconds)

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
        stages: dict = stages_dict(doc)
        stuck = []
        for key, val in stages.items():
            if not isinstance(val, dict):
                continue
            if val.get("status") not in _IN_FLIGHT:
                continue
            # Skip stages that started recently — they're presumed alive.
            # started_at is stored as an ISO string by stages_dict.
            started_at = val.get("started_at")
            if started_at:
                try:
                    parsed = datetime.fromisoformat(started_at)
                    # stages_dict emits naive UTC; compare on the same plane.
                    if parsed.tzinfo is not None:
                        parsed = parsed.replace(tzinfo=None)
                    if parsed > cutoff:
                        continue
                except (ValueError, TypeError):
                    pass  # unparseable → treat as old, reset
            stuck.append(key)
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
        doc.pipeline_state = compute_overall_state(stages_dict(doc))

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
            # Guard: if batch_analysis already finished for this batch, do NOT
            # clear the claim — that re-opens the CAS and lets stale
            # process_document_task replays re-fire the analyzer (ib-0001 loop).
            already_terminal = db.execute(
                text("""
                    SELECT 1 FROM document_pipeline_stages dps
                    JOIN documents d ON d.id = dps.document_id
                    WHERE d.ingest_batch_id = :bid
                      AND dps.stage = 'batch_analysis'
                      AND dps.status IN ('completed', 'failed', 'skipped')
                    LIMIT 1
                """),
                {"bid": batch.id},
            ).scalar()
            if already_terminal:
                continue
            # No docs are running and batch_analysis not yet done. Release claim.
            batch.analysis_queued_at = None
            if batch.status == IngestBatchStatus.PROCESSING:
                batch.status = IngestBatchStatus.PENDING
            recovered_ids.append(batch.id)

    if recovered_ids:
        db.commit()
        logger.info("recover_stuck_batches: released %d batch(es)", len(recovered_ids))

    return {"batches_recovered": len(recovered_ids), "batch_ids": recovered_ids}


def recover_unclaimed_ready_batches(db: Session) -> dict:
    """Claim and dispatch batches whose docs are ready for batch_analysis but
    were never queued.

    Closes the gap left by per-stage doc retries: those paths don't run
    process_document_task, so they never call claim_batch_for_analysis().
    The result is a batch with analysis_queued_at IS NULL even though every
    doc has completed extract + metadata. This sweep finds candidates and
    delegates to claim_batch_for_analysis() — its atomic UPDATE re-checks
    readiness, so we get the same race-safety as the inline trigger and a
    no-op when upstream isn't actually done.

    Returns {"batches_dispatched": N, "batch_ids": [...]}.
    """
    from app.services.intelligence.orchestrator import claim_batch_for_analysis
    from app.tasks.analyze_batch import analyze_batch_task
    from app.tasks.dispatch import dispatch_task

    rows = db.execute(
        text(
            """
            SELECT DISTINCT b.id
            FROM ingest_batches b
            JOIN documents d ON d.ingest_batch_id = b.id
            JOIN document_pipeline_stages dps ON dps.document_id = d.id
            WHERE b.analysis_queued_at IS NULL
              AND dps.stage = 'batch_analysis'
              AND dps.status = 'pending'
            """
        )
    ).fetchall()

    dispatched: list[int] = []
    for (batch_id,) in rows:
        if claim_batch_for_analysis(batch_id, db):
            dispatch_task(analyze_batch_task, batch_id)
            dispatched.append(batch_id)

    if dispatched:
        logger.info(
            "recover_unclaimed_ready_batches: dispatched %d batch(es): %s",
            len(dispatched),
            dispatched,
        )
    return {"batches_dispatched": len(dispatched), "batch_ids": dispatched}


# Gate-block skip reasons that produce recoverable SKIPPED rows — see
# enrich_document.py and extract_claims.py for context. Policy-skips
# ("ineligible_tier:administrative", etc.) are NOT listed here and stay SKIPPED.
_GATE_BLOCK_SKIP_REASONS = frozenset(
    {
        "batch_analysis_not_completed",
        "enrich_not_completed",
        "metadata_not_completed",
        "missing_ai_summary",
    }
)

# Stages downstream of ENRICH that are skipped with gate reasons when ENRICH
# is gate-blocked. We reset these alongside ENRICH so they can re-run once
# ENRICH completes.
_ENRICH_BLOCKED_DOWNSTREAM = (
    PipelineStage.CLAIMS,
    PipelineStage.RELATIONSHIPS,
    PipelineStage.ENTITIES,
)


def recover_stranded_gate_skipped(db: Session) -> dict:
    """Reset docs stranded by the gate-block-skip race and dispatch them.

    When a doc's ENRICH stage was marked SKIPPED with a gate-block reason
    (e.g. 'batch_analysis_not_completed') but BATCH_ANALYSIS has since
    completed, the doc is silently stuck: compute_overall_state treats
    SKIPPED as terminal so no recovery sweep picks it up, and
    _enrich_if_pending's CAS requires status='pending' — never satisfied.

    Finds these docs, resets ENRICH (and downstream gate-skipped stages) to
    PENDING, and dispatches enrich_document_task. Called once at startup
    after recover_orphaned_running_stages so any current restart unstrands
    victims of prior runs.

    Returns {"docs_recovered": N, "doc_ids": [...]}.
    """
    from app.tasks.dispatch import dispatch_task

    rows = db.execute(
        text(
            """
            SELECT DISTINCT dps.document_id
            FROM document_pipeline_stages dps
            WHERE dps.stage = 'enrich'
              AND dps.status = 'skipped'
              AND dps.reason = 'batch_analysis_not_completed'
              AND EXISTS (
                SELECT 1 FROM document_pipeline_stages dps2
                WHERE dps2.document_id = dps.document_id
                  AND dps2.stage = 'batch_analysis'
                  AND dps2.status IN ('completed', 'failed')
              )
            """
        )
    ).fetchall()

    recovered: list[int] = []
    for (doc_id,) in rows:
        # Reset ENRICH + cascade-skipped downstream stages to PENDING so the
        # CAS in claim_stage_for_dispatch can reclaim them.
        stages_to_reset = [PipelineStage.ENRICH, *_ENRICH_BLOCKED_DOWNSTREAM]
        for stage in stages_to_reset:
            _update_stage(
                doc_id,
                stage,
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

        # Dispatch via claim so concurrent workers don't double-fire.
        from app.tasks.enrich_document import enrich_document_task

        if claim_stage_for_dispatch(doc_id, PipelineStage.ENRICH, db):
            dispatch_task(enrich_document_task, doc_id)
            recovered.append(doc_id)

    if recovered:
        logger.info(
            "recover_stranded_gate_skipped: reset and redispatched %d doc(s): %s",
            len(recovered),
            recovered,
        )
    return {"docs_recovered": len(recovered), "doc_ids": recovered}


def recover_placeholder_summary_docs(db: Session) -> dict:
    """Reset and re-enrich docs whose ai_summary is the all-placeholder JSON.

    When the AI returns {"legal_significance":"...","required_action":"...",
    "financial_impact":"..."} the enricher now leaves ai_summary=NULL.  But
    docs enriched before that fix landed may have the placeholder string stored.
    Find those docs, reset their ENRICH stage to PENDING, and dispatch so they
    get real summaries on the next run.

    Called once at startup alongside recover_stranded_gate_skipped.
    Returns {"docs_recovered": N, "doc_ids": [...]}.
    """
    from app.tasks.dispatch import dispatch_task

    # The all-placeholder JSON string the old enricher stored verbatim.
    placeholder_summary = '{"legal_significance": "...", "required_action": "...", "financial_impact": "..."}'

    rows = db.execute(
        text(
            """
            SELECT d.id
            FROM documents d
            JOIN document_pipeline_stages dps ON dps.document_id = d.id
            WHERE d.ai_summary = :placeholder
              AND dps.stage = 'enrich'
              AND dps.status = 'completed'
            """
        ),
        {"placeholder": placeholder_summary},
    ).fetchall()

    recovered: list[int] = []
    for (doc_id,) in rows:
        _update_stage(
            doc_id,
            PipelineStage.ENRICH,
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
            commit=True,
        )
        from app.tasks.enrich_document import enrich_document_task

        if claim_stage_for_dispatch(doc_id, PipelineStage.ENRICH, db):
            dispatch_task(enrich_document_task, doc_id)
            recovered.append(doc_id)

    if recovered:
        logger.info(
            "recover_placeholder_summary_docs: reset and redispatched %d doc(s): %s",
            len(recovered),
            recovered,
        )
    return {"docs_recovered": len(recovered), "doc_ids": recovered}


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

    cutoff = naive_utc_now() - timedelta(seconds=max_age_seconds)

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

    # BATCH_ANALYSIS is batch-level (dispatch_arg="batch_id"); its claim +
    # dispatch is handled by recover_unclaimed_ready_batches above, not here.
    skip_stages = {PipelineStage.BATCH_ANALYSIS}

    redispatched: list[int] = []
    for doc in candidates:
        stages: dict = stages_dict(doc)

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
                # Batch-level stage: never dispatch from here, but it still
                # blocks downstream stages when not yet terminal. The original
                # unconditional `continue` was the bug that stranded ib-0033:
                # pending BATCH_ANALYSIS was invisible, so ENRICH was dispatched
                # prematurely, gate-blocked itself to SKIPPED, and the batch
                # analyzer's _enrich_if_pending CAS then found no PENDING row.
                stage_record = stages.get(spec.stage.value, {})
                status = (
                    stage_record.get("status")
                    if isinstance(stage_record, dict)
                    else None
                )
                if status not in (
                    StageStatus.COMPLETED.value,
                    StageStatus.SKIPPED.value,
                ):
                    break
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
    """Update a single stage row in document_pipeline_stages and recompute pipeline_state."""
    sk = stage.value
    assert sk.isidentifier(), f"pipeline_status: invalid stage key {sk!r}"

    for key in extra_sets:
        if key not in _ALLOWED_EXTRA_KEYS:
            raise ValueError(f"_update_stage: disallowed extra_sets key {key!r}")

    set_parts = ["status = :_status"]
    params: dict = {"_status": status.value, "_doc_id": doc_id, "_stage": sk}
    for key, val in extra_sets.items():
        if val is None:
            set_parts.append(f"{key} = NULL")
        else:
            pname = f"_x_{key}"
            set_parts.append(f"{key} = :{pname}")
            params[pname] = val

    result = db.execute(
        text(
            f"UPDATE document_pipeline_stages SET {', '.join(set_parts)} "
            f"WHERE document_id = :_doc_id AND stage = :_stage"
        ),
        params,
    )
    if result.rowcount == 0:
        # Guard: document may have been deleted between task dispatch and
        # execution (stale Celery task). If it's gone, skip the INSERT
        # rather than raising an IntegrityError on the FK constraint.
        doc_exists = db.execute(
            text("SELECT 1 FROM documents WHERE id = :id LIMIT 1"),
            {"id": doc_id},
        ).scalar()
        if doc_exists is None:
            logger.warning(
                "pipeline_status: doc %d not found — skipping stage insert "
                "(stage=%s status=%s)",
                doc_id,
                sk,
                status.value,
            )
            return
        ins: dict = {"_doc_id": doc_id, "_stage": sk, "_status": status.value}
        for k in _ALLOWED_EXTRA_KEYS:
            ins[f"_k_{k}"] = extra_sets.get(k)
        db.execute(
            text(
                "INSERT INTO document_pipeline_stages "
                "(document_id, stage, status, started_at, completed_at, error, reason, attempt, max_attempts, next_at) "
                "VALUES (:_doc_id, :_stage, :_status, :_k_started_at, :_k_completed_at, "
                ":_k_error, :_k_reason, :_k_attempt, :_k_max_attempts, :_k_next_at)"
            ),
            ins,
        )

    from app.models.database import Document

    doc_instance = db.get(Document, doc_id)
    if doc_instance is not None:
        db.expire(doc_instance, ["stage_rows"])

    rows = db.execute(
        text(
            "SELECT stage, status FROM document_pipeline_stages WHERE document_id = :_doc_id"
        ),
        {"_doc_id": doc_id},
    ).fetchall()
    current = {row[0]: {"status": row[1]} for row in rows}
    overall = compute_overall_state(current)
    db.execute(
        text("UPDATE documents SET pipeline_state = :state WHERE id = :doc_id"),
        {"state": overall.value, "doc_id": doc_id},
    )
    if commit:
        db.commit()


def is_db_locked(exc: Exception) -> bool:
    """True when an OperationalError represents a SQLITE_BUSY / locked-database error."""
    return "database is locked" in str(exc).lower()


def retry_on_db_locked(fn, db, *, attempts: int = 3, base_backoff: float = 0.05):
    """Run `fn()` with rollback+retry on SQLITE_BUSY_SNAPSHOT.

    WAL-mode SQLite returns "database is locked" immediately when a read
    snapshot can't upgrade to writer (Celery worker committed in between).
    busy_timeout doesn't apply to snapshot conflicts — rollback + retry does.

    Returns fn's return value, or re-raises the final OperationalError so the
    caller can decide between 409 and skip-and-continue.
    """
    last_exc: OperationalError | None = None
    for attempt in range(attempts):
        try:
            return fn()
        except OperationalError as exc:
            if not is_db_locked(exc):
                raise
            db.rollback()
            last_exc = exc
            if attempt < attempts - 1:
                time.sleep(base_backoff * (attempt + 1))
    assert last_exc is not None
    raise last_exc
