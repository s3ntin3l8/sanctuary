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
from collections.abc import Sequence
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from typing import Literal, cast

from sqlalchemy import text
from sqlalchemy.engine import CursorResult
from sqlalchemy.exc import OperationalError
from sqlalchemy.orm import Session

from app.core.timezone import ensure_utc, now_utc
from app.models.enums import PipelineStage, PipelineState, StageStatus

logger = logging.getLogger(__name__)

# Grace periods for recover_orphaned_running_stages' provable-orphan checks
# (see that function's docstring). Buffer past the authoritative deadline
# (task_time_limit for a RUNNING row, next_at for a RETRYING one) to absorb
# clock skew and the time it takes Celery's own kill/redelivery to land —
# not a tuning knob for "how patient to be," since these checks fire in
# addition to, not instead of, the softer heuristic gates.
_ORPHAN_PROVABLE_GRACE_SECONDS = 300
_RETRY_LOST_GRACE_SECONDS = 300
# A stage the sweeper has provably reset this many times (it keeps blowing the
# Celery time limit) is failed instead of reset again: a deterministic poison
# document would otherwise cycle reset -> redispatch -> hard kill forever and
# keep its case brief blocked (#150).
_ORPHAN_RESET_CAP = 3

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
        "orphan_resets",
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
    # True when the task atomically claims its own stage (pending→running) on
    # entry, rather than relying on the dispatcher to pre-claim. Recovery must
    # NOT pre-claim such stages: pre-claiming leaves the stage RUNNING and the
    # dispatched task then self-claims, sees RUNNING, and skips as
    # "already_claimed" — a permanent deadlock. Only metadata_task does this
    # (its normal dispatcher, process_document_task, hands it off unclaimed).
    #
    # EMBEDDINGS is a related but distinct case, NOT covered by this flag:
    # generate_embedding_task gates on METADATA being terminal and, if not,
    # returns early WITHOUT calling mark_started — leaving the stage PENDING
    # so metadata_task's own cascade can claim and redispatch it once
    # METADATA completes. Pre-claiming it when METADATA isn't terminal yet
    # would leave it RUNNING on defer, breaking that later re-claim — but
    # pre-claiming it when METADATA already IS terminal is fine (the task
    # won't defer). dispatch_pipeline_retry special-cases this directly by
    # checking METADATA's status, rather than via self_claims, since the
    # right answer depends on stage *state*, not just which stage it is.
    self_claims: bool = False


# Real dispatch dependencies (from the per-task .delay() chains):
#   process_document_task: EXTRACT only (ingest queue, concurrency configurable
#     via Settings, default 4), then dispatches metadata_task on the ai queue
#     and returns.
#   metadata_task: METADATA, then fans out to BATCH_ANALYSIS (gated on all
#     batch siblings' METADATA done) AND EMBEDDINGS (parallel).
#   analyze_batch_task: BATCH_ANALYSIS → ENRICH per doc.
#   enrich_document_task: ENRICH → RELATIONSHIPS, CLAIMS, ENTITIES (parallel
#     siblings; only RELATIONSHIPS is held by the per-case history gate).
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
        retry_task="app.tasks.document_processing.metadata_task",
        self_claims=True,  # metadata_task claims METADATA itself on entry
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
        depends_on=(PipelineStage.ENRICH,),
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
            "started_at": now_utc(),
            "attempt": None,
            "max_attempts": None,
            "next_at": None,
            "reason": None,  # e.g. a RELATIONSHIPS hold that has now been released
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
    if cast(CursorResult, result).rowcount == 0:
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
        extra_sets={"completed_at": now_utc(), "error": None, "orphan_resets": 0},
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
            "completed_at": now_utc(),
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
    next_at = ensure_utc(datetime.now(UTC) + timedelta(seconds=countdown))
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
    *,
    cascade: Sequence[PipelineStage] | None = None,
) -> None:
    """Mark `stage` failed and propagate failure to its per-doc downstream stages.

    Used when a stage's failure means downstream per-doc work cannot run
    (e.g. EXTRACT fails → METADATA, ENRICH, … cannot proceed).
    The cascade is not sticky — a successful retry naturally overwrites cascaded
    failed states as each stage runs and calls mark_started/mark_completed.

    `cascade` overrides the default `_DOWNSTREAM[stage]` list. Needed for
    METADATA specifically: its registry downstream includes BATCH_ANALYSIS,
    correct for reset_stage's retry-everything semantics, but wrong for a
    terminal give-up cascade — BATCH_ANALYSIS is a batch-shared stage (one
    analyze_batch_task per batch, not per doc), so marking this one doc's row
    FAILED would poison claim_batch_for_analysis's readiness check for every
    sibling and silently skip analyze() for the whole batch.
    """
    mark_failed(doc_id, stage, db, error=error, commit=False)
    for downstream in cascade if cascade is not None else _DOWNSTREAM.get(stage, []):
        _update_stage(
            doc_id,
            downstream,
            db,
            status=StageStatus.FAILED,
            extra_sets={
                "completed_at": now_utc(),
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


# METADATA's terminal-failure cascade must exclude BATCH_ANALYSIS, unlike
# STAGE_REGISTRY's generic downstream list (which reset_stage correctly uses
# in full — a retry legitimately wants BATCH_ANALYSIS redone too).
# BATCH_ANALYSIS is a batch-shared stage: one analyze_batch_task call covers
# every doc in the batch, not one call per doc. Marking this one doc's
# batch_analysis row FAILED would make claim_batch_for_analysis's readiness
# check (every doc's batch_analysis still unresolved) permanently false for
# the whole batch, and would trip metadata_task's "batch_already_done"
# fallback for healthy siblings — promoting their batch_analysis to
# COMPLETED without analyze() ever running, silently losing cover-letter
# detection and action-item extraction for the entire batch.
METADATA_FAILURE_CASCADE = (
    PipelineStage.ENRICH,
    PipelineStage.RELATIONSHIPS,
    PipelineStage.CLAIMS,
    PipelineStage.ENTITIES,
)

_IN_FLIGHT = (StageStatus.RUNNING.value, StageStatus.RETRYING.value)

_RESET_SETS: dict = {
    "started_at": None,
    "completed_at": None,
    "error": None,
    "reason": None,
    "attempt": None,
    "max_attempts": None,
    "next_at": None,
    "orphan_resets": 0,  # a user/pipeline retry starts the poison-doc count over
}


def clear_extraction_stamp(doc) -> None:
    """Drop the extractor stamp so a deliberate re-extract isn't short-circuited.

    ``process_document_task`` skips OCR when ``meta`` carries ``extractor`` +
    ``chunks`` (a guard against duplicate dispatches). A user-requested retry
    must pass that guard, so every EXTRACT reset clears the stamp first.
    """
    meta = dict(doc.meta or {})
    removed = [
        meta.pop(k, None)
        for k in (
            "extractor",
            "chunks",
            "page_failures",
            "ocr_unverified_pages",
            "ocr_unverified_acknowledged",
        )
    ]
    if any(v is not None for v in removed):
        doc.meta = meta


def reset_stage(
    doc_id: int, stage: PipelineStage, db: Session, *, force: bool = False
) -> bool:
    """Reset a stage (and its downstream dependents) to PENDING for retry.

    Returns True when ``stage`` was reset. Unless ``force`` is set, a stage that
    is RUNNING/RETRYING is left alone and False is returned: the status check
    is part of the UPDATE itself, so a dispatcher that claims the stage between
    a caller's read and this reset can never be clobbered back to PENDING
    (which would let two tasks run the same stage). Callers must dispatch only
    when this returns True.

    ``force=True`` is for a task resetting its *own* in-flight stage (e.g. the
    enrich gate deferring itself) — nobody else can legitimately own it then.
    It only lifts the guard for ``stage`` itself: a downstream stage that is in
    flight is never clobbered, forced or not.
    """
    guard = () if force else _IN_FLIGHT
    if not _update_stage(
        doc_id,
        stage,
        db,
        status=StageStatus.PENDING,
        extra_sets=dict(_RESET_SETS),
        commit=False,
        unless_status_in=guard,
    ):
        return False  # in flight: the guarded UPDATE matched nothing, nothing to undo
    if stage == PipelineStage.EXTRACT:
        from app.models.database import Document

        doc = db.get(Document, doc_id)
        if doc is not None:
            clear_extraction_stamp(doc)
    for downstream in _DOWNSTREAM.get(stage, []):
        # A downstream stage that is in flight keeps running; it re-runs on its
        # own schedule once its upstream completes again.
        _update_stage(
            doc_id,
            downstream,
            db,
            status=StageStatus.PENDING,
            extra_sets=dict(_RESET_SETS),
            commit=False,
            unless_status_in=_IN_FLIGHT,
        )
    db.commit()
    return True


def reset_all_stages(doc_id: int, db: Session) -> list[str]:
    """Reset every non-skipped stage to PENDING, all or nothing.

    Used by the document HUD's "retry all" action. SKIPPED stages stay skipped
    (e.g. BATCH_ANALYSIS on manually-uploaded docs); everything else is cleared
    of error/timestamps so the pipeline can run again from EXTRACT.

    Returns the stage keys that were RUNNING/RETRYING. If there are any, nothing
    is reset (the whole transaction is rolled back) so the caller can report a
    conflict; an empty list means every stage was reset and committed.
    """
    rows = db.execute(
        text(
            "SELECT stage, status, reason FROM document_pipeline_stages WHERE document_id = :doc_id"
        ),
        {"doc_id": doc_id},
    ).fetchall()
    if not rows:
        return []
    in_flight: list[str] = []
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

        if not _update_stage(
            doc_id,
            stage_enum,
            db,
            status=StageStatus.PENDING,
            extra_sets=dict(_RESET_SETS),
            commit=False,
            unless_status_in=_IN_FLIGHT,
        ):
            in_flight.append(stage_key)
    if in_flight:
        db.rollback()
        return in_flight
    from app.models.database import Document

    doc = db.get(Document, doc_id)
    if doc is not None:
        clear_extraction_stamp(doc)
    db.commit()
    return []


def reset_failed_stages_only(doc_id: int, db: Session) -> None:
    """Reset only FAILED pipeline stages to PENDING.

    Preserves COMPLETED and SKIPPED stages so a retry resumes from the
    first failed stage rather than re-running the entire pipeline from EXTRACT.
    Used by the queue's retry-failed action.

    Cascade-failed stages (status=failed, error="upstream X failed") are also
    reset — they become PENDING and will re-run once their upstream succeeds.
    Each update only applies while the row is still FAILED, so a stage another
    dispatcher has since re-claimed is never clobbered.
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
            extra_sets=dict(_RESET_SETS),
            commit=False,
            only_if_status_in=(StageStatus.FAILED.value,),
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


# --- RELATIONSHIPS history gate -----------------------------------------------
#
# Relationship detection links a document to *earlier documents of the same case*
# (Document.id < doc.id) — but only to ones that have been enriched (they need a
# significance tier and a summary). Every other stage looks at the document alone.
# So when many emails are ingested at once (a history import), only RELATIONSHIPS
# has to wait for the documents before it; everything else runs concurrently.

RELATIONSHIPS_HOLD_REASON = "waiting_for_earlier_documents"

# A document whose predecessors still haven't finished ENRICH this long after its
# own ENRICH completed is released anyway: one stuck document must not hold a
# case's relationships back forever (it just detects against fewer candidates).
RELATIONSHIPS_GATE_MAX_WAIT = timedelta(minutes=30)

_ENRICH_IN_FLIGHT = (
    StageStatus.PENDING.value,
    StageStatus.RUNNING.value,
    StageStatus.RETRYING.value,
)


def relationship_gate_case_id(db: Session, doc) -> str | None:
    """The case whose earlier documents are this document's relationship
    candidates (same resolution as relationship_detector._get_prior_docs).
    Unfiled mail ("_TRIAGE") has no case history to wait for."""
    from app.models.database import Proceeding

    case_id = doc.case_id
    if not case_id and doc.proceeding_id:
        case_id = (
            db.query(Proceeding.case_id)
            .filter(Proceeding.id == doc.proceeding_id)
            .scalar()
        )
    return None if not case_id or case_id == "_TRIAGE" else case_id


def relationships_gate_open(db: Session, doc_id: int) -> bool:
    """True when no earlier document of the same case still has ENRICH ahead of it.

    A batch that awaits the user's slice review can't finish by waiting, so its
    documents don't hold the gate. Past ``RELATIONSHIPS_GATE_MAX_WAIT`` since this
    document's own ENRICH completed, the gate opens regardless.
    """
    from app.models.database import (
        Document,
        DocumentPipelineStage,
        IngestBatch,
    )
    from app.models.enums import IngestBatchStatus

    doc = db.get(Document, doc_id)
    if doc is None:
        return True
    case_id = relationship_gate_case_id(db, doc)
    if case_id is None:
        return True

    blocked = (
        db.query(DocumentPipelineStage.document_id)
        .join(Document, Document.id == DocumentPipelineStage.document_id)
        .outerjoin(IngestBatch, IngestBatch.id == Document.ingest_batch_id)
        .filter(
            Document.case_id == case_id,
            Document.id < doc_id,
            DocumentPipelineStage.stage == PipelineStage.ENRICH.value,
            DocumentPipelineStage.status.in_(_ENRICH_IN_FLIGHT),
            (IngestBatch.status.is_(None))
            | (IngestBatch.status != IngestBatchStatus.AWAITING_SLICING),
        )
        .first()
        is not None
    )
    if not blocked:
        return True

    enriched_at = (
        db.query(DocumentPipelineStage.completed_at)
        .filter(
            DocumentPipelineStage.document_id == doc_id,
            DocumentPipelineStage.stage == PipelineStage.ENRICH.value,
        )
        .scalar()
    )
    if (
        enriched_at
        and now_utc() - ensure_utc(enriched_at) > RELATIONSHIPS_GATE_MAX_WAIT
    ):
        logger.warning(
            "Doc #%d: relationships released after %s — an earlier document's "
            "ENRICH never finished",
            doc_id,
            RELATIONSHIPS_GATE_MAX_WAIT,
        )
        return True
    return False


def hold_relationships(db: Session, doc_id: int) -> None:
    """Leave RELATIONSHIPS PENDING and say why (shown in the processing queue)."""
    db.execute(
        text(
            "UPDATE document_pipeline_stages SET reason = :reason "
            "WHERE document_id = :doc_id AND stage = :stage AND status = :pending"
        ),
        {
            "reason": RELATIONSHIPS_HOLD_REASON,
            "doc_id": doc_id,
            "stage": PipelineStage.RELATIONSHIPS.value,
            "pending": StageStatus.PENDING.value,
        },
    )
    db.commit()


def held_relationship_doc_ids(db: Session, case_id: str) -> list[int]:
    """Documents of ``case_id`` whose RELATIONSHIPS is held by the gate."""
    from app.models.database import Document, DocumentPipelineStage

    rows = (
        db.query(DocumentPipelineStage.document_id)
        .join(Document, Document.id == DocumentPipelineStage.document_id)
        .filter(
            Document.case_id == case_id,
            DocumentPipelineStage.stage == PipelineStage.RELATIONSHIPS.value,
            DocumentPipelineStage.status == StageStatus.PENDING.value,
            DocumentPipelineStage.reason == RELATIONSHIPS_HOLD_REASON,
        )
        .order_by(DocumentPipelineStage.document_id)
        .all()
    )
    return [row[0] for row in rows]


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


def _stage_status(db: Session, doc_id: int, stage: PipelineStage) -> str | None:
    return db.execute(
        text(
            "SELECT status FROM document_pipeline_stages "
            "WHERE document_id = :d AND stage = :s"
        ),
        {"d": doc_id, "s": stage.value},
    ).scalar()


def terminal_failure_cascade(stage: PipelineStage) -> tuple[PipelineStage, ...]:
    """Downstream stages to fail when ``stage`` fails terminally for one document.

    The registry's downstream list minus BATCH_ANALYSIS: that stage is shared by
    the whole batch, so failing it for one document would poison every sibling's
    readiness check (see ``METADATA_FAILURE_CASCADE``). Use this for any
    per-document give-up; reset_stage keeps the full list because a retry does
    want BATCH_ANALYSIS redone.

    The registry lists are transitive (EXTRACT's includes METADATA's own
    downstream), so a stage can be named twice when cascading; failing an
    already-failed row again is a harmless no-op.
    """
    return tuple(
        s for s in _DOWNSTREAM.get(stage, []) if s != PipelineStage.BATCH_ANALYSIS
    )


def _orphan_resets_so_far(db: Session, doc_id: int, stage: PipelineStage) -> int:
    return (
        db.execute(
            text(
                "SELECT orphan_resets FROM document_pipeline_stages "
                "WHERE document_id = :d AND stage = :s"
            ),
            {"d": doc_id, "s": stage.value},
        ).scalar()
        or 0
    )


def _fail_poison_stage(db: Session, doc, stage: PipelineStage) -> None:
    """Give up on a stage that keeps blowing the time limit: fail it for good.

    Cascades downstream the way any other terminal failure of that stage does,
    so the case brief isn't blocked on the document forever. BATCH_ANALYSIS is
    batch-shared, so every doc in the batch is failed and the batch claim is
    released (the same shape as analyze_batch's own timeout path); ENRICH then
    proceeds once its gate sees a terminal BATCH_ANALYSIS.
    """
    from app.models.database import IngestBatch

    error = (
        f"{stage.value} repeatedly exceeded the task time limit "
        f"({_ORPHAN_RESET_CAP} resets) — giving up"
    )
    logger.warning("Doc %d: %s", doc.id, error)
    if stage == PipelineStage.BATCH_ANALYSIS and doc.ingest_batch_id:
        batch = db.get(IngestBatch, doc.ingest_batch_id)
        if batch is not None:
            batch.analysis_queued_at = None
        sibling_ids = [
            row[0]
            for row in db.execute(
                text("SELECT id FROM documents WHERE ingest_batch_id = :b"),
                {"b": doc.ingest_batch_id},
            )
        ]
        for sibling_id in sibling_ids:
            # Leave siblings that already settled this stage alone — including
            # one that FAILED earlier, so its original cause is not overwritten.
            _update_stage(
                sibling_id,
                stage,
                db,
                status=StageStatus.FAILED,
                extra_sets={"completed_at": now_utc(), "error": error},
                commit=False,
                unless_status_in=(
                    StageStatus.COMPLETED.value,
                    StageStatus.SKIPPED.value,
                    StageStatus.FAILED.value,
                ),
            )
        db.commit()
        return
    mark_failed_with_cascade(
        doc.id, stage, db, error=error, cascade=terminal_failure_cascade(stage)
    )


def recover_orphaned_running_stages(
    db: Session,
    *,
    min_age_seconds: int = 1200,
    recent_activity_window_seconds: int = 1800,
) -> dict:
    """Reset pipeline stages stuck in RUNNING/RETRYING state past their
    expected runtime — presumed orphaned by a crashed worker.

    Originally written as a startup-only one-shot (when any RUNNING stage
    IS a crash artifact, since the worker has just rebooted). Now also
    runs on a 5-min cron via recover_pipeline_task — at which point an
    unbounded reset will kill legitimately-running long tasks (the
    batch_analyzer routinely runs 4-5 minutes; the enricher 2-3 min).

    Two gates keep cron-mode safe:

    1. `min_age_seconds` — only stages whose started_at is older than this
       threshold count as candidates. 20 min default covers the slowest
       legitimate single-stage call by ~3×.

    2. Worker-activity probe — even past min_age, a stage is only treated
       as orphaned when no OTHER stage anywhere has shown forward progress
       within `recent_activity_window_seconds`. "Forward progress" means a
       stage running with a recent started_at OR a stage completed with a
       recent completed_at. The rationale: gated LLM workers under shared-
       GPU contention legitimately spend 5-15 min waiting on the model_gate
       before their LLM call even starts, so a single stage's started_at
       can be stale while the cascade is genuinely alive. This gate
       prevents the runaway loop where the cron resets in-flight stages to
       PENDING, recover_stuck_pending_dispatches re-dispatches them, and
       the cycle repeats — the failure mode that produced 33-orphan-stages
       cycles and stranded 38 docs in mixed running/partial state.

    A stage that was *provably* orphaned (past the Celery time limit) more than
    ``_ORPHAN_RESET_CAP`` times is failed with a cascade instead of reset again,
    so a document that deterministically hangs the worker stops cycling.

    Returns {"docs_reset": N, "stages_reset": N, "stages_failed": N,
    "batches_reset": N} where ``batches_reset`` counts every batch that had a
    document touched (not only those whose BATCH_ANALYSIS was reset).
    """
    from app.config import CELERY_TASK_TIME_LIMIT, EXTRACT_TASK_TIME_LIMIT
    from app.models.database import Document, IngestBatch
    from app.models.enums import IngestBatchStatus

    cutoff = now_utc() - timedelta(seconds=min_age_seconds)

    # Provable threshold, independent of the heuristic gates below: past
    # task_time_limit (+ grace for the kill/redelivery to actually land),
    # Celery would already have hard-killed whatever worker held this stage
    # — there is no legitimate way for a RUNNING row to still be alive here,
    # no matter what any *other* worker in the system is doing. This closes
    # a gap the workers_recently_active probe can't: as long as anything
    # else in the system shows progress, that probe alone would never reset
    # a stage abandoned by a hard-killed worker, however old it gets.
    #
    # EXTRACT runs under its own, longer EXTRACT_TASK_TIME_LIMIT (see
    # app/config.py — it can stack a model_gate wait, Chandra's OCR budget,
    # and a Docling fallback pass in one task) — using the shared cutoff for
    # it would treat a still-legitimately-running EXTRACT task as provably
    # orphaned and steal its stage row out from under a live worker.
    _PROVABLE_ORPHAN_CUTOFF = now_utc() - timedelta(
        seconds=CELERY_TASK_TIME_LIMIT + _ORPHAN_PROVABLE_GRACE_SECONDS
    )
    _PROVABLE_ORPHAN_CUTOFF_EXTRACT = now_utc() - timedelta(
        seconds=EXTRACT_TASK_TIME_LIMIT + _ORPHAN_PROVABLE_GRACE_SECONDS
    )
    # A RETRYING row's own next_at is an explicit promise of when its retry
    # will fire — past that (plus grace for dispatch jitter), the dispatch
    # was lost, independent of task_time_limit or any global activity probe.
    _LOST_RETRY_CUTOFF = now_utc() - timedelta(seconds=_RETRY_LOST_GRACE_SECONDS)

    # Worker-activity probe: is any stage showing forward progress recently?
    # We sample globally because the relevant signal is "are workers making
    # progress at all" — a recent batch_analyzer completion implies the ai
    # worker is alive, which means a stale RUNNING enrich on a different
    # doc is also probably gate-waiting rather than orphaned.
    #
    # Skip the probe in startup mode (min_age_seconds <= 0): the call site
    # is asserting "workers just rebooted, no RUNNING row can be valid."
    # Without this bypass, the probe would find the candidate stage's own
    # RUNNING row as "recent activity" and refuse to reset it.
    if min_age_seconds > 0:
        activity_cutoff = now_utc() - timedelta(seconds=recent_activity_window_seconds)
        workers_recently_active = (
            db.execute(
                text(
                    "SELECT 1 FROM document_pipeline_stages "
                    "WHERE (status = :running AND started_at > :activity_cutoff) "
                    "   OR (status = :completed AND completed_at > :activity_cutoff) "
                    "LIMIT 1"
                ),
                {
                    "running": StageStatus.RUNNING.value,
                    "completed": StageStatus.COMPLETED.value,
                    "activity_cutoff": activity_cutoff,
                },
            ).scalar()
            is not None
        )
    else:
        workers_recently_active = False

    docs = (
        db.query(Document)
        .filter(Document.pipeline_state.in_(["running", "partial"]))
        .all()
    )

    docs_reset = 0
    stages_reset = 0
    stages_failed = 0
    affected_batch_ids: set[int] = set()
    batch_analysis_reset_ids: set[int] = set()

    for doc in docs:
        stages: dict = stages_dict(doc)
        stuck: list[tuple[str, bool]] = []  # (stage key, provably orphaned)
        for key, val in stages.items():
            if not isinstance(val, dict):
                continue
            status = val.get("status")
            if status not in _IN_FLIGHT:
                continue

            # Provable check first, independent of the heuristic gates below
            # and of min_age_seconds: past the authoritative deadline for
            # this row's status, it cannot legitimately still be in flight,
            # no matter what any *other* worker in the system is doing right
            # now. A RUNNING row past task_time_limit (+grace) means Celery
            # already killed whoever held it; a RETRYING row past its own
            # next_at (+grace) means the scheduled retry dispatch was lost.
            if status == StageStatus.RUNNING.value:
                started_at = val.get("started_at")
                if started_at:
                    try:
                        stage_cutoff = (
                            _PROVABLE_ORPHAN_CUTOFF_EXTRACT
                            if key == PipelineStage.EXTRACT.value
                            else _PROVABLE_ORPHAN_CUTOFF
                        )
                        if (
                            ensure_utc(datetime.fromisoformat(started_at))
                            < stage_cutoff
                        ):
                            stuck.append((key, True))
                            continue
                    except (ValueError, TypeError):
                        pass
            elif status == StageStatus.RETRYING.value:
                next_at = val.get("next_at")
                if next_at:
                    try:
                        if (
                            ensure_utc(datetime.fromisoformat(next_at))
                            < _LOST_RETRY_CUTOFF
                        ):
                            stuck.append((key, True))
                            continue
                    except (ValueError, TypeError):
                        pass

            # Heuristic gates (see docstring): skip stages that started
            # recently — they're presumed alive. started_at is stored as an
            # ISO string by stages_dict.
            started_at = val.get("started_at")
            if started_at:
                try:
                    parsed = ensure_utc(datetime.fromisoformat(started_at))
                    if parsed > cutoff:
                        continue
                except (ValueError, TypeError):
                    pass  # unparseable → treat as old, reset
            # Second gate (see docstring): even past min_age, presume the
            # stage is gate-waiting rather than orphaned when workers are
            # actively making progress elsewhere.
            if workers_recently_active:
                continue
            stuck.append((key, False))
        if not stuck:
            continue

        doc_stages_reset = 0
        for stage_key, provable in stuck:
            try:
                stage_enum = PipelineStage(stage_key)
            except ValueError:
                continue

            # The snapshot above may be stale by now: an earlier iteration for
            # this same document can have failed-and-cascaded this very stage,
            # or the task can have finished. Never overwrite a settled row.
            if _stage_status(db, doc.id, stage_enum) not in _IN_FLIGHT:
                continue

            # Only a *provable* orphan counts toward the cap: the heuristic
            # path also fires on every worker restart (min_age_seconds=0),
            # which says nothing about the document itself.
            if provable and _orphan_resets_so_far(db, doc.id, stage_enum) >= (
                _ORPHAN_RESET_CAP
            ):
                _fail_poison_stage(db, doc, stage_enum)
                stages_failed += 1
                continue

            # Reset only the stuck stage itself — do NOT cascade to already-completed
            # downstream stages. reset_stage() is for user-initiated retries where
            # re-running downstream is intentional; here we're just clearing an
            # in-flight lock left by a crash.
            if not _update_stage(
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
                only_if_status_in=_IN_FLIGHT,
            ):
                continue  # settled between the check above and this write
            if provable:
                db.execute(
                    text(
                        "UPDATE document_pipeline_stages "
                        "SET orphan_resets = orphan_resets + 1 "
                        "WHERE document_id = :d AND stage = :s"
                    ),
                    {"d": doc.id, "s": stage_enum.value},
                )
            stages_reset += 1
            doc_stages_reset += 1
            if stage_enum == PipelineStage.BATCH_ANALYSIS and doc.ingest_batch_id:
                batch_analysis_reset_ids.add(doc.ingest_batch_id)

        # Only a document where at least one reset actually landed counts as
        # "reset" (cap-failed stages are counted in stages_failed, and a stage
        # that settled since the snapshot changed nothing).
        if doc_stages_reset:
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
        "stages_failed": stages_failed,
        "batches_reset": len(affected_batch_ids),
    }


def recover_empty_batches(db: Session, *, max_age_seconds: int = 3600) -> dict:
    """Delete batches that never got (or lost) all their documents (#145).

    A batch with zero documents is invisible in the triage feed (it is built
    from documents), so it can never be reviewed or deleted from the UI, yet it
    used to sit in PENDING for ever: an upload that failed after its batch was
    committed, a batch whose ingest crashed mid-way, rows left by older code
    that did not roll back an all-duplicate email.

    Spared: the deliberate no-document tombstone that keeps a message from being
    re-imported (identified by ``meta.reason``, see ``NO_NEW_DOCUMENTS``, not by
    status) and AWAITING_SLICING batches (their documents are created when the
    user confirms the cuts). The age floor keeps a batch whose documents are
    still being created (``upload`` commits the batch first) out of the sweep.

    Returns {"batches_deleted": N, "batch_ids": [...]}.
    """
    from sqlalchemy import exists

    from app.models.database import Document, IngestBatch
    from app.models.enums import IngestBatchStatus
    from app.repositories.ingest_batch import is_not_tombstone_clause

    cutoff = now_utc() - timedelta(seconds=max_age_seconds)
    empty = (
        db.query(IngestBatch)
        .filter(
            IngestBatch.ingest_date < cutoff,
            IngestBatch.status != IngestBatchStatus.AWAITING_SLICING,
            is_not_tombstone_clause(),
            ~exists().where(Document.ingest_batch_id == IngestBatch.id),
        )
        .all()
    )
    ids = [b.id for b in empty]
    for batch in empty:
        db.delete(batch)
    if ids:
        db.commit()
        logger.info(
            "recover_empty_batches: deleted %d empty batch(es): %s", len(ids), ids
        )
    return {"batches_deleted": len(ids), "batch_ids": ids}


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
            # metadata_task replays re-fire the analyzer (ib-0001 loop).
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
    metadata_task, so they never call claim_batch_for_analysis().
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


def recover_unclaimed_ready_metadata_phases(db: Session) -> dict:
    """Claim + dispatch batches whose docs finished EXTRACT but never
    triggered the OCR→chat barrier (claim_batch_for_metadata_phase).

    Sibling to recover_unclaimed_ready_batches, same rationale: the barrier
    is triggered inline from process_document_task's terminal exit (success
    or failure). If that exit is interrupted before the check runs (worker
    killed between mark_completed/mark_failed_with_cascade and the barrier
    call), the batch is left with every doc's EXTRACT terminal but
    metadata_phase_queued_at still NULL — permanently stalled without this
    sweep. Delegates to claim_batch_for_metadata_phase(), whose atomic
    UPDATE re-checks readiness, so this is race-safe and a no-op when the
    inline trigger already fired normally.

    Straggler docs whose own metadata_task dispatch is lost mid-fan-out
    (rarer: crash inside the dispatch loop itself, after the batch claim
    already succeeded) are covered by the existing generic
    recover_stuck_pending_dispatches sweep — METADATA is self_claims=True,
    so redispatching it for any doc is always a safe no-op.

    Returns {"batches_dispatched": N, "batch_ids": [...]}.
    """
    from app.services.intelligence.orchestrator import (
        claim_batch_for_metadata_phase,
    )
    from app.tasks.document_processing import dispatch_metadata_phase

    rows = db.execute(
        text(
            """
            SELECT DISTINCT b.id
            FROM ingest_batches b
            JOIN documents d ON d.ingest_batch_id = b.id
            WHERE b.metadata_phase_queued_at IS NULL
              AND NOT EXISTS (
                SELECT 1 FROM documents d2
                WHERE d2.ingest_batch_id = b.id
                  AND NOT EXISTS (
                    SELECT 1 FROM document_pipeline_stages dps
                    WHERE dps.document_id = d2.id
                      AND dps.stage = 'extract'
                      AND dps.status IN ('completed', 'failed', 'skipped')
                  )
              )
            """
        )
    ).fetchall()

    dispatched: list[int] = []
    for (batch_id,) in rows:
        if claim_batch_for_metadata_phase(batch_id, db):
            dispatch_metadata_phase(batch_id, db)
            dispatched.append(batch_id)

    if dispatched:
        logger.info(
            "recover_unclaimed_ready_metadata_phases: dispatched %d batch(es): %s",
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


def recover_stranded_batch_pending(db: Session) -> dict:
    """Promote docs where batch_analysis=pending but batch siblings are done.

    These are left by 'retry metadata on a single doc in an already-analyzed
    batch': reset_stage cascades batch_analysis → PENDING, but
    claim_batch_for_analysis's idempotency guard (which blocks re-claim when
    any sibling has a terminal batch_analysis) prevents any task from ever
    claiming it. The enrich gate then defers forever.

    Finds these docs, marks their batch_analysis COMPLETED (the batch was
    already analyzed for the other docs), and dispatches enrich.

    Returns {"docs_recovered": N, "doc_ids": [...]}.
    """
    from app.tasks.dispatch import dispatch_task

    rows = db.execute(
        text(
            """
            SELECT dps.document_id
            FROM document_pipeline_stages dps
            JOIN documents d ON d.id = dps.document_id
            WHERE dps.stage = 'batch_analysis'
              AND dps.status = 'pending'
              AND d.ingest_batch_id IS NOT NULL
              AND EXISTS (
                  SELECT 1 FROM documents d2
                  JOIN document_pipeline_stages dps2 ON dps2.document_id = d2.id
                  WHERE d2.ingest_batch_id = d.ingest_batch_id
                    AND d2.id != d.id
                    AND dps2.stage = 'batch_analysis'
                    AND dps2.status IN ('completed', 'failed', 'skipped')
              )
            """
        )
    ).fetchall()

    promoted: list[int] = []
    for (doc_id,) in rows:
        mark_completed(doc_id, PipelineStage.BATCH_ANALYSIS, db, commit=False)
        promoted.append(doc_id)
    if promoted:
        db.commit()

    dispatched: list[int] = []
    for doc_id in promoted:
        from app.tasks.enrich_document import enrich_document_task

        if claim_stage_for_dispatch(doc_id, PipelineStage.ENRICH, db):
            dispatch_task(enrich_document_task, doc_id)
            dispatched.append(doc_id)

    if promoted:
        logger.info(
            "recover_stranded_batch_pending: promoted %d doc(s), dispatched enrich for %d: %s",
            len(promoted),
            len(dispatched),
            promoted,
        )
    return {"docs_recovered": len(dispatched), "doc_ids": dispatched}


def recover_stuck_pending_dispatches(
    db: Session,
    *,
    max_age_seconds: int = 60,
    recent_extract_window_seconds: int = 1800,
) -> dict:
    """Re-dispatch docs whose pipeline got stalled mid-cascade.

    Sibling to recover_orphaned_running_stages. Catches the EAGER+uvicorn-reload
    hazard: a stage's dispatch (or the chain of dispatches) got killed by
    uvicorn --reload. The doc is left with pipeline_state in (PENDING, PARTIAL)
    and one or more pending stages, but no stage is currently RUNNING (those
    are handled by the running-state recovery first).

    Resume strategy: dispatch EVERY pending stage whose whole upstream chain is
    completed/skipped, looking up each retry_task in STAGE_REGISTRY. Sibling
    stages (RELATIONSHIPS / CLAIMS / ENTITIES) are judged independently, so a
    held RELATIONSHIPS never hides a lost CLAIMS dispatch. RELATIONSHIPS is
    still filtered by ``relationships_gate_open``, EXTRACT by the
    ingest-worker-alive check. The retry_task is idempotent over already-completed
    upstream stages.

    A doc qualifies when:
      * pipeline_state in (PENDING, PARTIAL)
      * ingest_date < now - max_age_seconds (skip just-uploaded docs that
        haven't had a chance to run yet)
      * No stage is currently RUNNING (running-state recovery owns those)
      * At least one pending stage is ready (all of its upstream stages are terminal-ok)

    Special gate for EXTRACT (the first stage, FIFO ingest queue — concurrency
    is configurable via Settings, default 4, but the same ordering concern
    applies at any concurrency): "PENDING + ingest_date old + no stage
    RUNNING" is indistinguishable between "dispatch was lost" and "dispatch is
    still waiting in the queue behind a slow predecessor." Re-dispatching the
    latter caused a runaway loop (38 docs ingested, Chandra serialized → cron
    saw them all as stuck every 5 min, generated 900+ duplicate Chandra
    calls). To distinguish: if any
    EXTRACT stage anywhere completed within `recent_extract_window_seconds`
    (or is currently running), the ingest worker is presumed alive and any
    PENDING EXTRACTs are queue-waiting; skip them. If no recent EXTRACT
    activity, the worker is presumed down and recovery proceeds normally.

    Returns {"docs_redispatched": N, "doc_ids": [...]}.
    """
    import importlib
    from datetime import timedelta

    from app.models.database import Document

    cutoff = now_utc() - timedelta(seconds=max_age_seconds)

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

    # Probe: is the ingest worker presumed alive? If any EXTRACT stage is
    # currently running, or completed recently, the worker is processing
    # the queue and PENDING EXTRACTs are queue-waiting, not lost. See
    # docstring for the runaway-loop incident this gate prevents.
    extract_window_cutoff = now_utc() - timedelta(seconds=recent_extract_window_seconds)
    ingest_worker_alive = (
        db.execute(
            text(
                "SELECT 1 FROM document_pipeline_stages "
                "WHERE stage = :stage "
                "  AND (status = :running "
                "       OR (status = :completed AND completed_at > :cutoff)) "
                "LIMIT 1"
            ),
            {
                "stage": PipelineStage.EXTRACT.value,
                "running": StageStatus.RUNNING.value,
                "completed": StageStatus.COMPLETED.value,
                "cutoff": extract_window_cutoff,
            },
        ).scalar()
        is not None
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

        # Every PENDING stage whose whole upstream chain is terminal-ok is
        # ready to run. Siblings (RELATIONSHIPS / CLAIMS / ENTITIES) are judged
        # independently, so one held or failed sibling never hides another's
        # lost dispatch. BATCH_ANALYSIS is batch-level and dispatched by
        # recover_unclaimed_ready_batches above, but as a dependency it still
        # blocks ENRICH and everything after it until terminal.
        def _status(stage: PipelineStage, stages: dict = stages) -> str | None:
            record = stages.get(stage.value)
            return record.get("status") if isinstance(record, dict) else None

        ready_specs = [
            spec
            for spec in _STAGE_ORDER
            if spec.stage not in skip_stages
            and spec.dispatch_arg == "doc_id"
            and _status(spec.stage) == StageStatus.PENDING.value
            and all(
                _status(dep) in (StageStatus.COMPLETED.value, StageStatus.SKIPPED.value)
                for dep in _UPSTREAM[spec.stage]
            )
        ]

        doc_redispatched = False
        for head_spec in ready_specs:
            # Held by the history gate: an earlier document of the case is still
            # being enriched, so this stage is waiting on purpose, not lost. (The
            # gate opens by itself after RELATIONSHIPS_GATE_MAX_WAIT.)
            if (
                head_spec.stage == PipelineStage.RELATIONSHIPS
                and not relationships_gate_open(db, doc.id)
            ):
                continue

            # See docstring: a PENDING EXTRACT may be queue-waiting (legit) rather
            # than lost (recoverable). If the ingest worker is presumed alive
            # (recent extract activity), assume queue-waiting and skip.
            if head_spec.stage == PipelineStage.EXTRACT and ingest_worker_alive:
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

            # Self-claiming stages (metadata_task) must be dispatched UNCLAIMED:
            # the task flips PENDING→RUNNING itself on entry. Pre-claiming here
            # would leave the stage RUNNING with the dispatched task skipping as
            # "already_claimed" — the recovery deadlock that stranded ib-0001. The
            # task's own atomic claim provides the same double-dispatch dedup.
            if head_spec.self_claims:
                dispatch_task(task, doc.id)
                doc_redispatched = True
                continue

            # Atomic claim before dispatch — without this, a stage that is PENDING
            # at snapshot time but whose task is already queued (waiting behind a
            # busy worker) gets a second .delay() from us. Two concurrent extract_*
            # tasks then run on the same doc, the second deletes the first's
            # auto-claims via stale-cleanup, and if the second produces 0 the
            # first's work is gone. See doc_39 / 2026-05-26 22:00-22:12 incident.
            # claim_stage_for_dispatch is the same primitive every cascade
            # dispatcher uses (document_processing, enrich_document); recovery
            # was the outlier that skipped it. Stages whose task mark_starts
            # unconditionally (extract, enrich, …) rely on this pre-claim for dedup.
            if not claim_stage_for_dispatch(doc.id, head_spec.stage, db):
                logger.debug(
                    "Stuck-pending recovery: stage %s for doc %d already claimed "
                    "by another worker — skipping",
                    head_spec.stage.value,
                    doc.id,
                )
                continue

            dispatch_task(task, doc.id)
            doc_redispatched = True

        if doc_redispatched:
            redispatched.append(doc.id)

    return {"docs_redispatched": len(redispatched), "doc_ids": redispatched}


def recover_stuck_slicing_prep(
    db: Session, *, max_age_seconds: int | None = None
) -> dict:
    """Recover scan batches whose slicing preparation task was lost (#166).

    A multi-page scan sits in AWAITING_SLICING with ``meta.slicing.status ==
    "preparing"`` until ``prepare_slicing_task`` finishes. If the dispatch or the
    task is lost (worker crash, dropped message) nothing ever moves it on, and
    delete_bundle refuses AWAITING_SLICING batches, so it was stuck for good.

    ``meta.slicing.dispatched_at`` (stamped at every dispatch) separates "still
    running" from "lost": past one full task time limit plus grace the task
    cannot legitimately still be alive. A lost batch is re-dispatched once
    (``recovered``); if it is still ``preparing`` a threshold later it is marked
    failed, which the UI already offers Retry on and which makes the bundle
    deletable. A queue backlog can make "lost" a false positive, but the cost
    is one redundant preparation, and it is bounded to one.

    Returns {"redispatched": [...], "failed": [...]}.
    """
    from app.config import CELERY_TASK_TIME_LIMIT
    from app.models.database import IngestBatch
    from app.models.enums import IngestBatchStatus
    from app.tasks.dispatch import dispatch_task

    threshold = (
        max_age_seconds
        if max_age_seconds is not None
        else CELERY_TASK_TIME_LIMIT + _ORPHAN_PROVABLE_GRACE_SECONDS
    )
    now = now_utc()
    cutoff = now - timedelta(seconds=threshold)

    redispatched: list[int] = []
    failed: list[int] = []
    batches = (
        db.query(IngestBatch)
        .filter(IngestBatch.status == IngestBatchStatus.AWAITING_SLICING)
        .with_for_update(skip_locked=True)
        .all()
    )
    for batch in batches:
        slicing = dict((batch.meta or {}).get("slicing") or {})
        if slicing.get("status") != "preparing":
            continue
        stamped = slicing.get("dispatched_at")
        try:
            dispatched_at = (
                ensure_utc(datetime.fromisoformat(stamped)) if stamped else None
            )
        except (ValueError, TypeError):
            dispatched_at = None
        if dispatched_at is None:
            # Pre-dates the timestamp: the batch's own age is the best bound we
            # have, so an old one is treated as lost and recovered (instead of
            # being stamped "now", which would hide it from every later sweep).
            dispatched_at = ensure_utc(batch.ingest_date) or now
        if dispatched_at > cutoff:
            continue
        if not slicing.get("recovered"):
            slicing["recovered"] = True
            slicing["dispatched_at"] = now.isoformat()
            batch.meta = {**(batch.meta or {}), "slicing": slicing}
            db.commit()
            dispatch_task("app.tasks.prepare_slicing.prepare_slicing_task", batch.id)
            redispatched.append(batch.id)
        else:
            slicing["status"] = "failed"
            slicing["error"] = (
                "Slicing preparation never finished (the task was lost twice). "
                "Retry it, or delete the bundle."
            )
            batch.meta = {**(batch.meta or {}), "slicing": slicing}
            failed.append(batch.id)
    db.commit()
    if redispatched or failed:
        logger.warning(
            "recover_stuck_slicing_prep: re-dispatched %s, gave up on %s",
            redispatched,
            failed,
        )
    return {"redispatched": redispatched, "failed": failed}


def recover_embeddings_behind_failed_stage(
    db: Session, *, max_age_seconds: int = 300
) -> dict:
    """Settle EMBEDDINGS rows left PENDING on a document whose pipeline FAILED.

    A terminal failure of EXTRACT or METADATA cascades to ENRICH/RELATIONSHIPS/
    CLAIMS/ENTITIES but deliberately not to EMBEDDINGS (embeddings only need
    the extracted text), so the EMBEDDINGS row stays PENDING. Both stuck-
    dispatch sweeps skip FAILED documents, so nothing ever picks it up (#147).

    Scoped to EMBEDDINGS rather than "any PENDING stage on a FAILED doc": a
    generic sweep would loop, because generate_embedding_task leaves its stage
    PENDING on a gate block by design. Here the gate is checked up front — the
    task is only dispatched once METADATA is terminal (a failed METADATA is
    terminal), so it cannot defer — and every outcome leaves the row non-PENDING:

    * EXTRACT completed and METADATA terminal → claim the stage and dispatch
      generate_embedding_task (the document is still searchable).
    * EXTRACT did not complete → there is no text to embed: SKIPPED
      (``upstream_failed``).
    * Anything else (METADATA still in flight) → left alone; not stranded.

    Returns {"dispatched": N, "skipped": N, "doc_ids": [...]}.
    """
    cutoff = now_utc() - timedelta(seconds=max_age_seconds)
    doc_ids = [
        row[0]
        for row in db.execute(
            text(
                """
                SELECT d.id
                FROM documents d
                JOIN document_pipeline_stages emb
                  ON emb.document_id = d.id
                 AND emb.stage = :embeddings AND emb.status = :pending
                JOIN document_pipeline_stages f
                  ON f.document_id = d.id AND f.status = :failed
                 AND f.stage IN (:extract, :metadata)
                WHERE d.pipeline_state = :failed
                GROUP BY d.id
                HAVING MAX(f.completed_at) IS NULL OR MAX(f.completed_at) < :cutoff
                """
            ),
            {
                "embeddings": PipelineStage.EMBEDDINGS.value,
                "extract": PipelineStage.EXTRACT.value,
                "metadata": PipelineStage.METADATA.value,
                "pending": StageStatus.PENDING.value,
                "failed": StageStatus.FAILED.value,
                "cutoff": cutoff,
            },
        )
    ]
    if not doc_ids:
        return {"dispatched": 0, "skipped": 0, "doc_ids": []}

    from app.models.database import Document
    from app.tasks.dispatch import dispatch_task
    from app.tasks.generate_embedding import generate_embedding_task

    terminal = {
        StageStatus.COMPLETED.value,
        StageStatus.FAILED.value,
        StageStatus.SKIPPED.value,
    }
    dispatched = skipped = 0
    handled: list[int] = []
    for doc in db.query(Document).filter(Document.id.in_(doc_ids)).all():
        stages = stages_dict(doc)
        extract = (stages.get(PipelineStage.EXTRACT.value) or {}).get("status")
        metadata = (stages.get(PipelineStage.METADATA.value) or {}).get("status")
        if extract != StageStatus.COMPLETED.value:
            mark_skipped(doc.id, PipelineStage.EMBEDDINGS, db, reason="upstream_failed")
            skipped += 1
            handled.append(doc.id)
        elif metadata in terminal:
            if claim_stage_for_dispatch(doc.id, PipelineStage.EMBEDDINGS, db):
                dispatch_task(generate_embedding_task, doc.id)
                dispatched += 1
                handled.append(doc.id)
    if handled:
        logger.info(
            "recover_embeddings_behind_failed_stage: dispatched %d, skipped %d: %s",
            dispatched,
            skipped,
            handled,
        )
    return {"dispatched": dispatched, "skipped": skipped, "doc_ids": handled}


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
    only_if_status_in: tuple[str, ...] = (),
    unless_status_in: tuple[str, ...] = (),
) -> bool:
    """Update a single stage row in document_pipeline_stages and recompute pipeline_state.

    ``only_if_status_in`` / ``unless_status_in`` fold a status precondition into
    the UPDATE's WHERE clause so the check and the write are one atomic
    statement (a compare-and-swap, like ``claim_stage_for_dispatch``). Returns
    True when the stage row was written (or inserted), False when the
    precondition failed or the document is gone.
    """
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

    where = "document_id = :_doc_id AND stage = :_stage"
    for i, val in enumerate(only_if_status_in):
        params[f"_only_{i}"] = val
    if only_if_status_in:
        marks = ", ".join(f":_only_{i}" for i in range(len(only_if_status_in)))
        where += f" AND status IN ({marks})"
    for i, val in enumerate(unless_status_in):
        params[f"_unless_{i}"] = val
    if unless_status_in:
        marks = ", ".join(f":_unless_{i}" for i in range(len(unless_status_in)))
        where += f" AND status NOT IN ({marks})"

    result = db.execute(
        text(
            f"UPDATE document_pipeline_stages SET {', '.join(set_parts)} WHERE {where}"
        ),
        params,
    )
    if cast(CursorResult, result).rowcount == 0:
        if only_if_status_in or unless_status_in:
            exists = db.execute(
                text(
                    "SELECT 1 FROM document_pipeline_stages "
                    "WHERE document_id = :_doc_id AND stage = :_stage"
                ),
                {"_doc_id": doc_id, "_stage": sk},
            ).scalar()
            if exists is not None:
                return False  # row present but its status failed the guard
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
            return False
        ins: dict = {"_doc_id": doc_id, "_stage": sk, "_status": status.value}
        for k in _ALLOWED_EXTRA_KEYS:
            ins[f"_k_{k}"] = extra_sets.get(k)
        db.execute(
            text(
                "INSERT INTO document_pipeline_stages "
                "(document_id, stage, status, started_at, completed_at, error, reason, "
                "attempt, max_attempts, next_at, orphan_resets) "
                "VALUES (:_doc_id, :_stage, :_status, :_k_started_at, :_k_completed_at, "
                ":_k_error, :_k_reason, :_k_attempt, :_k_max_attempts, :_k_next_at, "
                "COALESCE(:_k_orphan_resets, 0))"
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
    return True


def is_db_locked(exc: Exception) -> bool:
    """True when an OperationalError represents a transient write-write
    conflict worth retrying: SQLite's SQLITE_BUSY (legacy — "database is
    locked") or Postgres's deadlock/serialization-failure errors (SQLSTATE
    40P01 / 40001 — two concurrent writers both touched the same document's
    stage rows and one lost the conflict). Postgres surfaces these via
    `psycopg.errors.DeadlockDetected` / `SerializationFailure`, wrapped by
    SQLAlchemy as `OperationalError` like everything else DBAPI-level.
    """
    msg = str(exc).lower()
    return (
        "database is locked" in msg
        or "deadlock detected" in msg
        or "could not serialize access" in msg
    )


def retry_on_db_locked(fn, db, *, attempts: int = 3, base_backoff: float = 0.05):
    """Run `fn()` with rollback+retry on a transient write-write conflict.

    Two Celery workers (or a worker + an interactive request) committing
    against the same document's stage rows at the same moment can lose to a
    deadlock or serialization conflict — rollback + retry recovers instead of
    surfacing a 500/409 for what's really just contention.

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
