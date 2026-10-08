"""The processing queue: which documents the workers are handling, for the
SPA's queue popover (app/api/v1/worker_queue.py) and the shell badge."""

import logging

from sqlalchemy import or_
from sqlalchemy.exc import OperationalError
from sqlalchemy.orm import Query, Session

from app.models.database import Document, User
from app.models.enums import PipelineStage, PipelineState
from app.services import access_service
from app.services.pipeline_status import STAGE_REGISTRY, stages_dict

logger = logging.getLogger(__name__)


def _visible_to(
    query: Query, owner_id: int | None, visible_case_ids: set[str] | None
) -> Query:
    """Restrict a Document query to docs owned by `owner_id` or whose case is
    in `visible_case_ids`. No-ops when `owner_id` is None (unrestricted
    context) or when `visible_case_ids` is None (admin)."""
    if owner_id is None or visible_case_ids is None:
        return query
    return query.filter(
        or_(Document.owner_id == owner_id, Document.case_id.in_(visible_case_ids))
    )


def _get_queue_docs(
    db: Session, owner_id: int | None = None
) -> tuple[list[Document], list[Document], list[Document]]:
    """Return (running_docs, pending_docs, failed_docs) ordered for display,
    restricted to what `owner_id` may see (None = unrestricted).

    PARTIAL docs (some stages done, some pending/running) are included in the
    running bucket so their active stages are visible in the panel.  Without
    this, a doc whose pipeline_state flips to PARTIAL mid-processing (e.g.
    between stage transitions) disappears from the queue entirely.
    """
    visible_case_ids = (
        access_service.visible_case_ids(db, db.get(User, owner_id))
        if owner_id is not None
        else None
    )
    active = (
        _visible_to(
            db.query(Document).filter(
                Document.pipeline_state.in_(
                    [
                        PipelineState.RUNNING,
                        PipelineState.PARTIAL,
                        PipelineState.PENDING,
                    ]
                )
            ),
            owner_id,
            visible_case_ids,
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
        _visible_to(
            db.query(Document).filter(Document.pipeline_state == PipelineState.FAILED),
            owner_id,
            visible_case_ids,
        )
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


def retry_failed_docs_for(db: Session, user: User) -> None:
    """Reset and re-dispatch every FAILED doc the user may see."""
    from app.services.pipeline_status import (
        _DOWNSTREAM,
        _UPSTREAM,
        STAGE_REGISTRY,
        reset_failed_stages_only,
        retry_on_db_locked,
        stages_dict,
    )
    from app.services.triage_retry import dispatch_pipeline_retry
    from app.tasks.dispatch import dispatch_task

    visible_case_ids = access_service.visible_case_ids(db, user)
    failed_docs = _visible_to(
        db.query(Document).filter(Document.pipeline_state == PipelineState.FAILED),
        user.id,
        visible_case_ids,
    ).all()
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
            # earliest failed stage's own dispatch convention (self-claiming
            # vs. pre-claimed vs. batch-shared) is honored by
            # dispatch_pipeline_retry — in particular this dedupes
            # BATCH_ANALYSIS via claim_batch_for_analysis, so multiple docs
            # in the same batch sharing it as their earliest failed stage
            # don't each independently dispatch analyze_batch_task.
            earliest = min(failed_specs, key=lambda s: s.order)
            logger.info(
                "retry-failed: doc %d dispatching earliest failed stage: %s",
                doc_id,
                earliest.stage.value,
            )
            dispatch_pipeline_retry(doc_id, doc.ingest_batch_id, earliest.stage, db)
            # Sibling stages (e.g. RELATIONSHIPS and CLAIMS both fan out of
            # ENRICH) don't chain off each other, so a failed sibling whose
            # upstream is intact needs its own dispatch. Stages downstream of
            # `earliest` are re-run by it and are left alone.
            rerun = {earliest.stage, *_DOWNSTREAM.get(earliest.stage, ())}
            for spec in failed_specs:
                if spec.stage in rerun or spec.dispatch_arg != "doc_id":
                    continue
                if all(
                    pre_reset.get(dep.value, {}).get("status")
                    in ("completed", "skipped")
                    for dep in _UPSTREAM[spec.stage]
                ):
                    dispatch_pipeline_retry(doc_id, doc.ingest_batch_id, spec.stage, db)
        else:
            # No recognised failed stage — fall back to head task (EXTRACT).
            from app.tasks.document_processing import process_document_task

            logger.warning(
                "retry-failed: doc %d has no identifiable failed stage; "
                "dispatching head task",
                doc_id,
            )
            dispatch_task(process_document_task, doc_id)
