"""The processing queue: what the workers are doing for this user's documents."""

from __future__ import annotations

from fastapi import APIRouter, Depends, Request
from sqlalchemy.orm import Session

from app.api.worker_queue import (
    _build_queue_items,
    _first_failed_stage_info,
    _get_queue_docs,
    retry_failed_docs_for,
)
from app.core.rate_limit import limiter
from app.dependencies import get_current_user, get_db
from app.models.database import Document, User
from app.schemas.worker_queue import FailedDoc, QueueCounts, QueueItem, QueueView
from app.services.ai_inflight import count_inflight

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
    return QueueItem(
        kind="doc",
        stage=item["stage"],
        label=_doc_label(doc),
        doc_id=doc.id,
        batch_id=doc.ingest_batch_id,
        doc_count=1,
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
    return QueueView(
        counts=QueueCounts(
            executing=len(executing),
            queued=len(queued),
            failed=len(failed_docs),
            ai_inflight=count_inflight(),
        ),
        executing=executing,
        queued=queued,
        failed=failed_docs,
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
