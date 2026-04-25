"""Atomic per-stage pipeline-status tracker.

Each mutation issues a single SQL UPDATE using SQLite's json_set so that
concurrent Celery workers writing different stages of the same document
never race each other via Python read-modify-write.
"""

import logging
from datetime import UTC, datetime

from sqlalchemy import text
from sqlalchemy.orm import Session

from app.models.enums import PipelineStage, PipelineState, StageStatus

logger = logging.getLogger(__name__)

# DAG order used for upstream-running guard in retry endpoint.
# Stages earlier in this list block stages later in it when running.
_STAGE_ORDER: list[PipelineStage] = [
    PipelineStage.EXTRACT,
    PipelineStage.METADATA,
    PipelineStage.BATCH_ANALYSIS,
    PipelineStage.ENRICH,
    PipelineStage.RELATIONSHIPS,
    PipelineStage.CLAIMS,
    PipelineStage.ENTITIES,
    PipelineStage.EMBEDDINGS,
]

# Stages that, when retried, should also reset their dependents to PENDING.
_DOWNSTREAM: dict[PipelineStage, list[PipelineStage]] = {
    PipelineStage.EXTRACT: [
        PipelineStage.METADATA,
        PipelineStage.ENRICH,
        PipelineStage.RELATIONSHIPS,
        PipelineStage.CLAIMS,
        PipelineStage.ENTITIES,
    ],
    PipelineStage.METADATA: [
        PipelineStage.ENRICH,
        PipelineStage.RELATIONSHIPS,
        PipelineStage.CLAIMS,
        PipelineStage.ENTITIES,
    ],
    PipelineStage.ENRICH: [
        PipelineStage.RELATIONSHIPS,
        PipelineStage.CLAIMS,
        PipelineStage.ENTITIES,
    ],
    PipelineStage.BATCH_ANALYSIS: [],
    PipelineStage.RELATIONSHIPS: [],
    PipelineStage.CLAIMS: [],
    PipelineStage.ENTITIES: [],
    PipelineStage.EMBEDDINGS: [],
}


def _now_iso() -> str:
    return datetime.now(UTC).isoformat()


def _stage_record(status: StageStatus, **extra) -> dict:
    return {"status": status.value, **extra}


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
    )


def mark_completed(doc_id: int, stage: PipelineStage, db: Session) -> None:
    _update_stage(
        doc_id,
        stage,
        db,
        status=StageStatus.COMPLETED,
        extra_sets={"completed_at": _now_iso(), "error": None},
    )


def mark_failed(
    doc_id: int, stage: PipelineStage, db: Session, error: str = ""
) -> None:
    _update_stage(
        doc_id,
        stage,
        db,
        status=StageStatus.FAILED,
        extra_sets={"completed_at": _now_iso(), "error": error},
    )


def mark_skipped(
    doc_id: int, stage: PipelineStage, db: Session, reason: str = ""
) -> None:
    _update_stage(
        doc_id, stage, db, status=StageStatus.SKIPPED, extra_sets={"reason": reason}
    )


def reset_stage(doc_id: int, stage: PipelineStage, db: Session) -> None:
    """Reset a stage (and its downstream dependents) to PENDING for retry."""
    stages_to_reset = [stage] + _DOWNSTREAM.get(stage, [])
    for s in stages_to_reset:
        _update_stage(doc_id, s, db, status=StageStatus.PENDING, extra_sets={})


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
    idx = _STAGE_ORDER.index(stage)
    upstream = _STAGE_ORDER[:idx]
    blocking = []
    for s in upstream:
        record = stages.get(s.value, {})
        if record.get("status") == StageStatus.RUNNING.value:
            blocking.append(s.value)
    return blocking


# ---------------------------------------------------------------------------
# Internal helpers
# ---------------------------------------------------------------------------


def _update_stage(
    doc_id: int,
    stage: PipelineStage,
    db: Session,
    status: StageStatus,
    extra_sets: dict,
) -> None:
    """Update a single stage key inside pipeline_stages and recompute pipeline_state."""
    import json

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
    db.commit()
