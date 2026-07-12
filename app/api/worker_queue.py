import logging

from fastapi import APIRouter, Depends, Request
from sqlalchemy import text
from sqlalchemy.exc import OperationalError
from sqlalchemy.orm import Session

from app.config import templates
from app.core.rate_limit import limiter
from app.dependencies import get_db
from app.models.database import Document
from app.models.enums import PipelineStage, PipelineState
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


def _executing_stages(doc: Document) -> list[str]:
    """Stages where a worker is actively processing RIGHT NOW (status=running).

    Distinct from _active_stages, which also includes 'retrying' — those are
    waiting for a countdown, not executing. For the queue-panel split into
    'Executing' vs 'Queued' sections, only truly-running stages count as
    executing; retrying stages display as queued."""
    return [
        key
        for key, rec in _ordered_stages(doc)
        if isinstance(rec, dict) and rec.get("status") == "running"
    ]


def _retrying_stages(doc: Document) -> list[str]:
    """Stages waiting for a Celery retry countdown — visible in the panel but
    classified as queued for the executing/queued split."""
    return [
        key
        for key, rec in _ordered_stages(doc)
        if isinstance(rec, dict) and rec.get("status") == "retrying"
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

    Each item carries an `executing` flag — True when the stage is RUNNING
    (a worker is actively processing it right now), False when it's
    queued/retrying/blocked by a gate. The flag drives both the badge
    counts and the executing/queued split in the panel template.

    Running docs: emit one item per RUNNING stage (executing=True). If the
    doc has no RUNNING stage but is in PARTIAL/RUNNING DB state, emit an
    item for the first retrying or pending stage as queued.
    Pending docs: one item per doc showing the next stage to run as queued.
    Batch-scoped stages (BATCH_ANALYSIS) are grouped into one item per batch.
    """
    items: list[dict] = []
    batch_buckets: dict[tuple, int] = {}

    def _add_stage(doc: Document, stage: str, executing: bool) -> None:
        spec = STAGE_REGISTRY.get(PipelineStage(stage)) if stage else None
        if spec and spec.dispatch_arg == "batch_id" and doc.ingest_batch_id is not None:
            key = (stage, doc.ingest_batch_id)
            if key in batch_buckets:
                items[batch_buckets[key]]["docs"].append(doc)
                # Upgrade to executing if any sibling is RUNNING — the
                # batch-level task either is or isn't running, no mixed state.
                if executing:
                    items[batch_buckets[key]]["executing"] = True
            else:
                batch_buckets[key] = len(items)
                items.append(
                    {
                        "type": "batch",
                        "batch": doc.ingest_batch,
                        "stage": stage,
                        "executing": executing,
                        "docs": [doc],
                    }
                )
        else:
            items.append(
                {"type": "doc", "doc": doc, "stage": stage, "executing": executing}
            )

    for doc in running:
        executing = _executing_stages(doc)
        retrying = _retrying_stages(doc)
        if executing:
            for stage in executing:
                _add_stage(doc, stage, executing=True)
            # Also surface any concurrent retrying stages as queued items.
            for stage in retrying:
                _add_stage(doc, stage, executing=False)
        elif retrying:
            for stage in retrying:
                _add_stage(doc, stage, executing=False)
        else:
            # Fallback: doc is RUNNING/PARTIAL in DB but no stage has a
            # non-terminal status yet (transition window); show first
            # pending stage instead, as queued.
            stage = _first_pending_stage(doc)
            if stage:
                _add_stage(doc, stage, executing=False)

    for doc in pending:
        stage = _first_pending_stage(doc)
        if stage:
            _add_stage(doc, stage, executing=False)

    return items


def compute_queue_counts(db: Session) -> dict[str, int]:
    """Single source of truth for worker-queue badge and popover counts.

    Stage-level counts (one per running/retrying stage, one per pending
    doc's first stage) so the rail badge matches the popover's "X Active"
    header exactly. n_failed stays per-document — a failed doc is one
    failure regardless of which stage tripped it.
    """
    running, pending, failed = _get_queue_docs(db)
    queue_items = _build_queue_items(running, pending)
    return {
        "n_executing": sum(1 for item in queue_items if item.get("executing")),
        "n_queued": sum(1 for item in queue_items if not item.get("executing")),
        "n_failed": len(failed),
    }


@router.get("/badge")
async def worker_queue_badge(request: Request, db: Session = Depends(get_db)):
    _fail_fast_reads(db)
    counts = compute_queue_counts(db)
    return templates.TemplateResponse(
        request,
        "partials/_worker_queue_badge.html",
        {
            "n_active": counts["n_executing"] + counts["n_queued"],
            "n_failed": counts["n_failed"],
        },
    )


@router.get("/panel")
async def worker_queue_panel_body(request: Request, db: Session = Depends(get_db)):
    _fail_fast_reads(db)
    running, pending, failed = _get_queue_docs(db)
    queue_items = _build_queue_items(running, pending)
    n_active_ai = count_inflight()
    # Executing vs queued counts derive directly from queue_items so badges
    # match what the panel actually renders. "Executing" = a worker is
    # processing this item right now (status=running); everything else
    # (pending, retrying, blocked on a gate) counts as queued.
    n_executing = sum(1 for item in queue_items if item.get("executing"))
    n_queued = sum(1 for item in queue_items if not item.get("executing"))
    # Split the items list so the template can render an "Executing" section
    # and a "Queued" section without re-filtering.
    executing_items = [item for item in queue_items if item.get("executing")]
    queued_items = [item for item in queue_items if not item.get("executing")]
    failed_doc_errors = {doc.id: _first_failed_stage_info(doc) for doc in failed}
    return templates.TemplateResponse(
        request,
        "partials/_worker_queue_panel_body.html",
        {
            "executing_items": executing_items,
            "queued_items": queued_items,
            "failed_docs": failed,
            "failed_doc_errors": failed_doc_errors,
            "n_active_ai": n_active_ai,
            "n_executing": n_executing,
            "n_queued": n_queued,
            "n_failed": len(failed),
        },
    )


@router.post("/retry-failed")
@limiter.limit("5/minute")
async def retry_failed_docs(request: Request, db: Session = Depends(get_db)):
    import importlib

    from app.services.pipeline_status import (
        STAGE_REGISTRY,
        reset_failed_stages_only,
        retry_on_db_locked,
        stages_dict,
    )
    from app.tasks.dispatch import dispatch_task

    failed_docs = (
        db.query(Document).filter(Document.pipeline_state == PipelineState.FAILED).all()
    )
    for doc in failed_docs:
        doc_id = doc.id

        # Snapshot stage states BEFORE reset — reset turns FAILED → PENDING,
        # so we must read which stages were FAILED first.
        db.refresh(doc)
        pre_reset = stages_dict(doc)

        try:
            retry_on_db_locked(lambda _id=doc_id: reset_failed_stages_only(_id, db), db)
        except OperationalError:
            logger.warning(
                "retry-failed: doc %d still locked after retries; skipping", doc_id
            )
            continue

        # Find the lowest-order failed stage and dispatch its registered task,
        # so CLAIMS-only failures go straight to extract_claims_task instead of
        # re-running Docling from the beginning.
        failed_specs = [
            spec
            for stage, spec in STAGE_REGISTRY.items()
            if pre_reset.get(stage.value, {}).get("status") == "failed"
        ]
        if failed_specs:
            earliest = min(failed_specs, key=lambda s: s.order)
            arg = doc.ingest_batch_id if earliest.dispatch_arg == "batch_id" else doc_id
            module_name, func_name = earliest.retry_task.rsplit(".", 1)
            task = getattr(importlib.import_module(module_name), func_name)
            logger.info(
                "retry-failed: doc %d dispatching %s (earliest failed stage: %s)",
                doc_id,
                func_name,
                earliest.stage.value,
            )
            dispatch_task(task, arg)
        else:
            # No recognised failed stage — fall back to head task (EXTRACT).
            from app.tasks.document_processing import process_document_task

            logger.warning(
                "retry-failed: doc %d has no identifiable failed stage; "
                "dispatching head task",
                doc_id,
            )
            dispatch_task(process_document_task, doc_id)

    running, pending, failed = _get_queue_docs(db)
    queue_items = _build_queue_items(running, pending)
    n_active_ai = count_inflight()
    n_executing = sum(1 for item in queue_items if item.get("executing"))
    n_queued = sum(1 for item in queue_items if not item.get("executing"))
    executing_items = [item for item in queue_items if item.get("executing")]
    queued_items = [item for item in queue_items if not item.get("executing")]
    failed_doc_errors = {doc.id: _first_failed_stage_info(doc) for doc in failed}
    return templates.TemplateResponse(
        request,
        "partials/_worker_queue_panel_body.html",
        {
            "executing_items": executing_items,
            "queued_items": queued_items,
            "failed_docs": failed,
            "failed_doc_errors": failed_doc_errors,
            "n_active_ai": n_active_ai,
            "n_executing": n_executing,
            "n_queued": n_queued,
            "n_failed": len(failed),
        },
    )
