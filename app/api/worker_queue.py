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
    """Return (running_docs, pending_docs, failed_docs) ordered for display.

    PARTIAL docs (some stages done, some pending/running) are included in the
    running bucket so their active stages are visible in the panel.  Without
    this, a doc whose pipeline_state flips to PARTIAL mid-processing (e.g.
    between stage transitions) disappears from the queue entirely.
    """
    active = (
        db.query(Document)
        .filter(
            Document.pipeline_state.in_(
                [PipelineState.RUNNING, PipelineState.PARTIAL, PipelineState.PENDING]
            )
        )
        .order_by(Document.pipeline_state)
        .limit(50)
        .all()
    )
    running = [
        d
        for d in active
        if d.pipeline_state in (PipelineState.RUNNING, PipelineState.PARTIAL)
    ]
    pending = [d for d in active if d.pipeline_state == PipelineState.PENDING]
    failed = (
        db.query(Document)
        .filter(Document.pipeline_state == PipelineState.FAILED)
        .limit(20)
        .all()
    )
    return running, pending, failed


def _ordered_stages(doc: Document):
    """Return stage items sorted descending by pipeline order (highest first)."""
    return sorted(
        stages_dict(doc).items(),
        key=lambda kv: STAGE_REGISTRY[kv[0]].order if kv[0] in STAGE_REGISTRY else -1,
        reverse=True,
    )


def _active_stages(doc: Document) -> list[str]:
    """Return every stage currently running or retrying, highest pipeline order first.

    A doc can have multiple concurrent active stages — e.g. ENTITIES and CLAIMS
    both run in parallel after ENRICH completes.
    """
    return [
        key
        for key, rec in _ordered_stages(doc)
        if isinstance(rec, dict) and rec.get("status") in ("running", "retrying")
    ]


def _first_pending_stage(doc: Document) -> str:
    """Return the earliest (lowest-order) pending stage, or empty string."""
    for key, rec in reversed(_ordered_stages(doc)):
        if isinstance(rec, dict) and rec.get("status") == "pending":
            return key
    return ""


def _first_failed_stage_info(doc: Document) -> dict:
    """Return the root-cause failed stage name and error text.

    Sorts ascending by pipeline order so the earliest (root-cause) failure
    is found first, not a downstream cascade whose error reads
    "upstream {stage} failed".
    Returns {"stage": str, "error": str}; both empty if no failed stage found.
    """
    ordered = sorted(
        stages_dict(doc).items(),
        key=lambda kv: STAGE_REGISTRY[kv[0]].order if kv[0] in STAGE_REGISTRY else 99,
    )
    for key, rec in ordered:
        if isinstance(rec, dict) and rec.get("status") == "failed":
            return {"stage": key, "error": rec.get("error") or ""}
    return {"stage": "", "error": ""}


def _build_queue_items(running: list[Document], pending: list[Document]) -> list[dict]:
    """Build display items for the queue panel.

    Running docs: one item per active stage — a doc with ENTITIES + CLAIMS both
    running produces two separate rows so every concurrent stage is visible.
    Batch-scoped stages (BATCH_ANALYSIS) are grouped into one item per batch.
    Pending docs: one item per doc showing the next stage to run.
    """
    items: list[dict] = []
    batch_buckets: dict[tuple, int] = {}

    def _add_stage(doc: Document, stage: str) -> None:
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

    for doc in running:
        active = _active_stages(doc)
        if active:
            for stage in active:
                _add_stage(doc, stage)
        else:
            # Fallback: doc is RUNNING in DB but no stage has running status yet
            # (transition window); show first pending stage instead.
            stage = _first_pending_stage(doc)
            if stage:
                _add_stage(doc, stage)

    for doc in pending:
        stage = _first_pending_stage(doc)
        if stage:
            _add_stage(doc, stage)

    return items


@router.get("/badge")
async def worker_queue_badge(request: Request, db: Session = Depends(get_db)):
    _fail_fast_reads(db)
    n_queue = (
        db.query(Document)
        .filter(
            Document.pipeline_state.in_(
                [PipelineState.RUNNING, PipelineState.PARTIAL, PipelineState.PENDING]
            )
        )
        .count()
    )
    n_failed = (
        db.query(Document)
        .filter(Document.pipeline_state == PipelineState.FAILED)
        .count()
    )
    return templates.TemplateResponse(
        request,
        "partials/_worker_queue_badge.html",
        {"n_queue": n_queue, "n_failed": n_failed},
    )


@router.get("/panel")
async def worker_queue_panel_body(request: Request, db: Session = Depends(get_db)):
    _fail_fast_reads(db)
    running, pending, failed = _get_queue_docs(db)
    queue_items = _build_queue_items(running, pending)
    n_active_ai = count_inflight()
    # Use COUNT(*) for accurate totals — _get_queue_docs caps at 50 docs so
    # len(running) would understate the real count when the queue is deep.
    # PARTIAL docs count as running: they have completed some stages and are
    # mid-pipeline (active stage may be PENDING or RUNNING within PARTIAL).
    n_running = (
        db.query(Document)
        .filter(
            Document.pipeline_state.in_([PipelineState.RUNNING, PipelineState.PARTIAL])
        )
        .count()
    )
    n_pending = (
        db.query(Document)
        .filter(Document.pipeline_state == PipelineState.PENDING)
        .count()
    )
    failed_doc_errors = {doc.id: _first_failed_stage_info(doc) for doc in failed}
    return templates.TemplateResponse(
        request,
        "partials/_worker_queue_panel_body.html",
        {
            "queue_items": queue_items,
            "failed_docs": failed,
            "failed_doc_errors": failed_doc_errors,
            "n_active_ai": n_active_ai,
            "n_running": n_running,
            "n_pending": n_pending,
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
    n_active_ai = count_inflight()
    n_running = (
        db.query(Document)
        .filter(Document.pipeline_state == PipelineState.RUNNING)
        .count()
    )
    n_pending = (
        db.query(Document)
        .filter(Document.pipeline_state == PipelineState.PENDING)
        .count()
    )
    failed_doc_errors = {doc.id: _first_failed_stage_info(doc) for doc in failed}
    return templates.TemplateResponse(
        request,
        "partials/_worker_queue_panel_body.html",
        {
            "queue_items": queue_items,
            "failed_docs": failed,
            "failed_doc_errors": failed_doc_errors,
            "n_active_ai": n_active_ai,
            "n_running": n_running,
            "n_pending": n_pending,
            "n_failed": len(failed),
        },
    )
