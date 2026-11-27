"""Re-run one pipeline stage of a document (the per-stage Retry button).

Shared by ``POST /documents/{id}/pipeline/{stage}/retry`` and the second-opinion
confirm of an ``ocr_text`` finding (an EXTRACT retry). Knows nothing about
HTTP: a refusal raises :class:`RetryRefused`.
"""

from __future__ import annotations

from sqlalchemy import text
from sqlalchemy.orm import Session

from app.models.database import Document
from app.models.enums import PipelineStage
from app.services.pipeline_status import (
    STAGE_REGISTRY,
    get_upstream_blocking,
    reset_stage,
    stages_dict,
)
from app.services.triage_retry import dispatch_pipeline_retry, rearm_batch_barriers


class RetryRefused(Exception):
    """The stage cannot be retried now (HTTP 409 equivalent)."""

    def __init__(self, code: str, detail: str) -> None:
        super().__init__(detail)
        self.code = code
        self.detail = detail


def lock_document_row(doc_id: int, db: Session) -> None:
    db.execute(
        text("SELECT id FROM documents WHERE id = :doc_id FOR UPDATE"),
        {"doc_id": doc_id},
    )


def retry_document_stage(db: Session, doc: Document, stage: PipelineStage) -> None:
    """Reset ``stage`` (BATCH_ANALYSIS: every failed sibling of the batch) and
    dispatch it again; an EXTRACT retry re-arms the batch barriers first."""
    lock_document_row(doc.id, db)
    db.refresh(doc)
    stages = stages_dict(doc)
    current = (stages.get(stage.value) or {}).get("status")
    if current in ("running", "retrying"):
        raise RetryRefused("in_flight", f"Stage '{stage.value}' is already {current}.")
    blocking = get_upstream_blocking(stage, stages)
    if blocking:
        labels = ", ".join(STAGE_REGISTRY[PipelineStage(b)].label for b in blocking)
        raise RetryRefused(
            "upstream_running",
            f"Waiting for {labels} — {STAGE_REGISTRY[stage].label} re-runs automatically afterwards.",
        )
    if stage == PipelineStage.BATCH_ANALYSIS and doc.ingest_batch_id:
        siblings = (
            db.query(Document)
            .filter(Document.ingest_batch_id == doc.ingest_batch_id)
            .all()
        )
        reset_any = False
        claimed_since_read: list[int] = []
        for sibling in siblings:
            if (stages_dict(sibling).get("batch_analysis") or {}).get(
                "status"
            ) == "failed":
                if reset_stage(sibling.id, PipelineStage.BATCH_ANALYSIS, db):
                    reset_any = True
                else:
                    claimed_since_read.append(sibling.id)
        if claimed_since_read and not reset_any:
            # Every failed sibling was re-claimed by another dispatcher after the
            # read above: that retry is already running, don't start a second.
            raise RetryRefused(
                "in_flight",
                "Batch analysis is already being retried "
                f"(documents {', '.join(map(str, claimed_since_read))}).",
            )
    elif not reset_stage(doc.id, stage, db):
        # A dispatcher claimed the stage after the status check above.
        raise RetryRefused("in_flight", f"Stage '{stage.value}' is already running.")
    if stage == PipelineStage.EXTRACT and doc.ingest_batch_id:
        rearm_batch_barriers(doc.ingest_batch_id, db)
    db.refresh(doc)
    dispatch_pipeline_retry(doc.id, doc.ingest_batch_id, stage, db)
