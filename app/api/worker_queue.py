import logging

from fastapi import APIRouter, Depends, Request
from sqlalchemy.exc import OperationalError
from sqlalchemy.orm import Session

from app.config import templates
from app.dependencies import get_db
from app.models.database import Document
from app.models.enums import PipelineState

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/api/worker/queue", tags=["worker-queue"])


def _get_queue_docs(
    db: Session,
) -> tuple[list[Document], list[Document], list[Document]]:
    """Return (running_docs, pending_docs, failed_docs) ordered for display."""
    active = (
        db.query(Document)
        .filter(
            Document.pipeline_state.in_([PipelineState.RUNNING, PipelineState.PENDING])
        )
        .order_by(Document.pipeline_state)
        .limit(50)
        .all()
    )
    running = [d for d in active if d.pipeline_state == PipelineState.RUNNING]
    pending = [d for d in active if d.pipeline_state == PipelineState.PENDING]
    failed = (
        db.query(Document)
        .filter(Document.pipeline_state == PipelineState.FAILED)
        .limit(20)
        .all()
    )
    return running, pending, failed


def _current_stage(doc: Document) -> str:
    """Return the stage name currently running/retrying, or first pending, or empty."""
    stages = doc.pipeline_stages or {}
    for stage_key, rec in stages.items():
        if isinstance(rec, dict) and rec.get("status") in ("running", "retrying"):
            return stage_key
    for stage_key, rec in stages.items():
        if isinstance(rec, dict) and rec.get("status") == "pending":
            return stage_key
    return ""


@router.get("/badge")
async def worker_queue_badge(request: Request, db: Session = Depends(get_db)):
    running, pending, _ = _get_queue_docs(db)
    n_active = len(running) + len(pending)
    return templates.TemplateResponse(
        request,
        "partials/_worker_queue_badge.html",
        {"n_active": n_active},
    )


@router.get("/panel")
async def worker_queue_panel_body(request: Request, db: Session = Depends(get_db)):
    running, pending, failed = _get_queue_docs(db)
    docs_with_stage = [(doc, _current_stage(doc)) for doc in running + pending + failed]
    n_active = len(running) + len(pending)
    return templates.TemplateResponse(
        request,
        "partials/_worker_queue_panel_body.html",
        {
            "running_docs": running,
            "pending_docs": pending,
            "failed_docs": failed,
            "docs_with_stage": docs_with_stage,
            "n_active": n_active,
            "n_running": len(running),
            "n_pending": len(pending),
            "n_failed": len(failed),
        },
    )


@router.post("/retry-failed")
async def retry_failed_docs(request: Request, db: Session = Depends(get_db)):
    from app.services.pipeline_status import reset_all_stages, retry_on_db_locked
    from app.tasks.dispatch import dispatch_task
    from app.tasks.document_processing import process_document_task

    failed_docs = (
        db.query(Document).filter(Document.pipeline_state == PipelineState.FAILED).all()
    )
    for doc in failed_docs:
        doc_id = doc.id
        try:
            retry_on_db_locked(lambda _id=doc_id: reset_all_stages(_id, db), db)
        except OperationalError:
            logger.warning(
                "retry-failed: doc %d still locked after retries; skipping", doc_id
            )
            continue
        dispatch_task(process_document_task, doc_id)

    running, pending, failed = _get_queue_docs(db)
    docs_with_stage = [(doc, _current_stage(doc)) for doc in running + pending + failed]
    n_active = len(running) + len(pending)
    return templates.TemplateResponse(
        request,
        "partials/_worker_queue_panel_body.html",
        {
            "running_docs": running,
            "pending_docs": pending,
            "failed_docs": failed,
            "docs_with_stage": docs_with_stage,
            "n_active": n_active,
            "n_running": len(running),
            "n_pending": len(pending),
            "n_failed": len(failed),
        },
    )
