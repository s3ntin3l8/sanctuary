"""The processing queue: what the workers are doing for this user's documents."""

from __future__ import annotations

from fastapi import APIRouter, Depends, Request
from sqlalchemy.orm import Session

from app.core.rate_limit import limiter
from app.dependencies import get_current_user, get_db
from app.models.database import Document, User
from app.models.enums import PipelineStage
from app.schemas.worker_queue import FailedDoc, QueueCounts, QueueItem, QueueView
from app.services import gmail_import_status
from app.services.ai_inflight import count_inflight
from app.services.pipeline_status import RELATIONSHIPS_HOLD_REASON, stages_dict
from app.services.worker_queue import (
    _build_queue_items,
    _first_failed_stage_info,
    _get_queue_docs,
    retry_failed_docs_for,
)

router = APIRouter(prefix="/worker-queue", tags=["worker-queue"])


def _doc_label(doc: Document) -> str:
    return doc.title or doc.original_filename or f"Doc #{doc.id}"


def _queue_item(item: dict) -> QueueItem:
    if item["type"] == "batch":
        batch = item["batch"]
        return QueueItem(
            kind="batch",
            stage=item["stage"],
            label=f"Batch #{batch.id} — {(batch.subject or '')[:40]}".rstrip(" —"),
            doc_id=None,
            batch_id=batch.id,
            doc_count=len(item["docs"]),
        )
    doc = item["doc"]
    held = (
        item["stage"] == PipelineStage.RELATIONSHIPS
        and stages_dict(doc).get(PipelineStage.RELATIONSHIPS.value, {}).get("reason")
        == RELATIONSHIPS_HOLD_REASON
    )
    return QueueItem(
        kind="doc",
        stage=item["stage"],
        label=_doc_label(doc),
        doc_id=doc.id,
        batch_id=doc.ingest_batch_id,
        doc_count=1,
        note="Waiting for earlier documents of the case" if held else None,
    )


def queue_view(db: Session, user: User) -> QueueView:
    running, pending, failed = _get_queue_docs(db, owner_id=user.id)
    raw_items = _build_queue_items(running, pending)
    executing = [_queue_item(i) for i in raw_items if i["executing"]]
    queued = [_queue_item(i) for i in raw_items if not i["executing"]]
    failed_docs = []
    for doc in failed:
        info = _first_failed_stage_info(doc)
        failed_docs.append(
            FailedDoc(
                doc_id=doc.id,
                batch_id=doc.ingest_batch_id,
                title=_doc_label(doc),
                stage=info["stage"],
                error=info["error"],
            )
        )
    gmail_import = gmail_import_status.recent_import_status(user.id)
    # Messages an import has yet to fetch are queued work too, so the rail badge
    # reflects them instead of showing idle while the import is between emails.
    awaiting = (
        max(0, gmail_import.total - gmail_import.done)
        if gmail_import and gmail_import.active
        else 0
    )
    return QueueView(
        counts=QueueCounts(
            executing=len(executing),
            queued=len(queued) + awaiting,
            failed=len(failed_docs),
            ai_inflight=count_inflight(),
        ),
        executing=executing,
        queued=queued,
        failed=failed_docs,
        gmail_import=gmail_import,
    )


@router.get("", response_model=QueueView)
def worker_queue(db: Session = Depends(get_db), user: User = Depends(get_current_user)):
    return queue_view(db, user)


@router.post("/retry-failed", response_model=QueueView)
@limiter.limit("5/minute")
def retry_failed(
    request: Request,
    db: Session = Depends(get_db),
    user: User = Depends(get_current_user),
):
    retry_failed_docs_for(db, user)
    return queue_view(db, user)
