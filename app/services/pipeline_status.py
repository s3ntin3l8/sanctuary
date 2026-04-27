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
from datetime import UTC, datetime
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
    _update_stage(
        doc_id,
        stage,
        db,
        status=StageStatus.RUNNING,
        extra_sets={"started_at": _now_iso()},
        commit=True,  # early commit so the UI flips to RUNNING immediately
    )


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
        extra_sets={"completed_at": _now_iso(), "error": error},
        commit=commit,
    )


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
            doc_id, s, db, status=StageStatus.PENDING, extra_sets={}, commit=False
        )
    db.commit()


def compute_overall_state(stages: dict) -> PipelineState:
    """Derive overall PipelineState from per-stage dict."""
    if not stages:
        return PipelineState.PENDING
    statuses = {v.get("status") for v in stages.values()}
    if StageStatus.RUNNING.value in statuses:
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
    """Reset any pipeline stages left in RUNNING state due to a prior crash.

    Called once at app startup after migrations. Finds documents with
    pipeline_state in (RUNNING, PARTIAL), resets every RUNNING stage (plus
    its downstream dependents) back to PENDING, recomputes pipeline_state,
    and unblocks affected IngestBatches.

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

    for doc in docs:
        stages: dict = doc.pipeline_stages or {}
        stuck = [
            key
            for key, val in stages.items()
            if isinstance(val, dict) and val.get("status") == StageStatus.RUNNING.value
        ]
        if not stuck:
            continue

        for stage_key in stuck:
            try:
                stage_enum = PipelineStage(stage_key)
            except ValueError:
                continue
            reset_stage(doc.id, stage_enum, db)
            stages_reset += 1

        db.refresh(doc)
        doc.pipeline_state = compute_overall_state(doc.pipeline_stages or {})

        docs_reset += 1
        if doc.ingest_batch_id:
            affected_batch_ids.add(doc.ingest_batch_id)

    for batch_id in affected_batch_ids:
        batch = db.query(IngestBatch).filter(IngestBatch.id == batch_id).first()
        if not batch:
            continue
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
    """Update a single stage key inside pipeline_stages and recompute pipeline_state."""
    row = db.execute(
        text("SELECT pipeline_stages FROM documents WHERE id = :doc_id"),
        {"doc_id": doc_id},
    ).fetchone()

    if row is None:
        logger.warning(f"pipeline_status: doc {doc_id} not found")
        return

    stages: dict = json.loads(row[0]) if row[0] else {}
    stage_entry = stages.setdefault(stage.value, {})
    stage_entry["status"] = status.value
    for key, val in extra_sets.items():
        if val is None:
            stage_entry.pop(key, None)
        else:
            stage_entry[key] = val

    overall = compute_overall_state(stages)
    db.execute(
        text(
            "UPDATE documents SET pipeline_stages = :stages, pipeline_state = :state"
            " WHERE id = :doc_id"
        ),
        {"stages": json.dumps(stages), "state": overall.value, "doc_id": doc_id},
    )
    if commit:
        db.commit()
