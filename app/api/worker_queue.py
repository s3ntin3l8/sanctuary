import logging

from fastapi import APIRouter, Depends, Request
from sqlalchemy import text
from sqlalchemy.exc import OperationalError
from sqlalchemy.orm import Session

from app.config import templates
from app.core.rate_limit import limiter
from app.dependencies import get_db
from app.models.database import Document
from app.models.enums import PipelineState
from app.services.ai_inflight import count_inflight
from app.services.pipeline_status import STAGE_REGISTRY, stages_dict

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/api/worker/queue", tags=["worker-queue"])


# Read endpoints have a sub-second latency budget. Override the connection's
# global busy_timeout (60s, see app/config.py) so a contended read fails fast
# with OperationalError instead of holding the HTTP connection open. The UI
# layer surfaces the failure as a "Worker busy — retry" state.
_READ_BUSY_TIMEOUT_MS = 1000


def _fail_fast_reads(db: Session) -> None:
    db.execute(text(f"PRAGMA busy_timeout = {_READ_BUSY_TIMEOUT_MS}"))


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
    stages = stages_dict(doc)
    for stage_key, rec in stages.items():
        if isinstance(rec, dict) and rec.get("status") in ("running", "retrying"):
            return stage_key
    for stage_key, rec in stages.items():
        if isinstance(rec, dict) and rec.get("status") == "pending":
            return stage_key
    return ""


def _build_queue_items(running: list[Document], pending: list[Document]) -> list[dict]:
    """Group batch-scoped stages into one item per batch; leave others as individual doc items.

    Preserves running-before-pending ordering; batch groups appear at the position
    of their first member doc. Uses STAGE_REGISTRY.dispatch_arg == "batch_id" as the
    canonical signal so future batch-scoped stages are grouped automatically.
    """
    items: list[dict] = []
    batch_buckets: dict[tuple, int] = {}

    for doc in running + pending:
        stage = _current_stage(doc)
        spec = STAGE_REGISTRY.get(stage) if stage else None
        if spec and spec.dispatch_arg == "batch_id" and doc.ingest_batch_id is not None:
            key = (stage, doc.ingest_batch_id)
            if key in batch_buckets:
                items[batch_buckets[key]]["docs"].append(doc)
            else:
                batch_buckets[key] = len(items)
                items.append(
                    {
                        "type": "batch",
                        "batch": doc.ingest_batch,
                        "stage": stage,
                        "docs": [doc],
                    }
                )
        else:
            items.append({"type": "doc", "doc": doc, "stage": stage})

    return items


@router.get("/badge")
async def worker_queue_badge(request: Request, db: Session = Depends(get_db)):
    _fail_fast_reads(db)
    n_active = count_inflight()
    return templates.TemplateResponse(
        request,
        "partials/_worker_queue_badge.html",
        {"n_active": n_active},
    )


@router.get("/panel")
async def worker_queue_panel_body(request: Request, db: Session = Depends(get_db)):
    _fail_fast_reads(db)
    running, pending, failed = _get_queue_docs(db)
    queue_items = _build_queue_items(running, pending)
    n_active = count_inflight()
    return templates.TemplateResponse(
        request,
        "partials/_worker_queue_panel_body.html",
        {
            "queue_items": queue_items,
            "failed_docs": failed,
            "n_active": n_active,
            "n_running": len(running),
            "n_pending": len(pending),
            "n_failed": len(failed),
        },
    )


@router.post("/retry-failed")
@limiter.limit("5/minute")
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
    queue_items = _build_queue_items(running, pending)
    n_active = count_inflight()
    return templates.TemplateResponse(
        request,
        "partials/_worker_queue_panel_body.html",
        {
            "queue_items": queue_items,
            "failed_docs": failed,
            "n_active": n_active,
            "n_running": len(running),
            "n_pending": len(pending),
            "n_failed": len(failed),
        },
    )
