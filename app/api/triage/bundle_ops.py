"""Bundle-level mutations: dismiss, delete, retry, retry-all, get bundle, pipeline status."""

from __future__ import annotations

import json
import logging
import time

from fastapi import APIRouter, Depends, Form, HTTPException, Request
from fastapi.responses import HTMLResponse
from sqlalchemy.exc import OperationalError
from sqlalchemy.orm import Session

from app.config import templates
from app.constants import ORIGINATOR_COLORS, ORIGINATOR_ICONS
from app.core.rate_limit import limiter
from app.dependencies import get_db
from app.models.enums import OriginatorType, UserReactionType
from app.services.triage_bundles import get_bundle_by_batch_id, get_triage_bundles
from app.services.triage_dismissal import delete_bundle as _delete_bundle
from app.services.triage_dismissal import dismiss_bundle as _dismiss_bundle
from app.services.triage_oob_render import (
    render_bundle_group_oob,
    render_sidebar_badges_oob,
    render_triage_feed_oob,
    render_triage_header_stats_oob,
)
from app.services.triage_reactions import get_reactions_by_doc_ids
from app.services.triage_retry import dispatch_batch_retry, reset_batch_for_retry

logger = logging.getLogger(__name__)

router = APIRouter()


@router.post("/triage/dismiss")
async def dismiss_bundle(
    batch_id: int | None = None,
    doc_id: int | None = None,
    db: Session = Depends(get_db),
):
    success = _dismiss_bundle(db, batch_id=batch_id, doc_id=doc_id)
    if not success:
        raise HTTPException(status_code=404, detail="Bundle or document not found")

    # Return OOB swap to delete the row
    target_id = (
        f"triage-row-batch-{batch_id}" if batch_id else f"triage-row-loose-{doc_id}"
    )
    html = f'<div id="{target_id}" hx-swap-oob="delete"></div>'

    # If triage is now empty, OOB-replace the entire feed with its empty state.
    bundles = get_triage_bundles(db, limit=1)
    if not bundles:
        empty_feed = templates.get_template("partials/triage_feed.html").render(
            bundles=[], as_oob=True
        )
        return HTMLResponse(content=html + empty_feed)

    return HTMLResponse(content=html)


@router.post("/triage/delete")
async def delete_bundle(
    batch_id: int | None = None,
    doc_id: int | None = None,
    db: Session = Depends(get_db),
):
    try:
        success = _delete_bundle(db, batch_id=batch_id, doc_id=doc_id)
    except ValueError as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc
    if not success:
        raise HTTPException(status_code=404, detail="Bundle or document not found")

    target_id = (
        f"triage-row-batch-{batch_id}" if batch_id else f"triage-row-loose-{doc_id}"
    )
    html = f'<div id="{target_id}" hx-swap-oob="delete"></div>'

    bundles = get_triage_bundles(db, limit=1)
    if not bundles:
        empty_feed = templates.get_template("partials/triage_feed.html").render(
            bundles=[], as_oob=True
        )
        return HTMLResponse(content=html + empty_feed)

    return HTMLResponse(content=html)


@router.post("/triage/bundle/retry")
@limiter.limit("20/minute")
async def retry_bundle_pipeline(
    request: Request,
    batch_id: int = Form(...),
    full: str = Form("false"),
    db: Session = Depends(get_db),
):
    """Re-run every AI stage for every doc in the bundle.

    Skips EXTRACT (preserves manual case assignments) and leaves SKIPPED stages
    alone. Clears analysis_queued_at so the batch analysis can re-fire.
    Returns 409 if any stage is actively running.
    """
    from app.models.database import IngestBatch
    from app.services.pipeline_status import retry_on_db_locked

    is_full = full.lower() == "true"

    batch = db.query(IngestBatch).filter(IngestBatch.id == batch_id).first()
    if not batch:
        raise HTTPException(status_code=404, detail=f"Batch {batch_id} not found")

    def _do_reset():
        result = reset_batch_for_retry(batch, db, full=is_full)
        if result == -1:
            return -1, None, None
        items, batch_fallback = result
        db.commit()
        return items, batch_fallback, len(batch.documents)

    try:
        items, batch_fallback, doc_count = retry_on_db_locked(_do_reset, db)
    except OperationalError as exc:
        raise HTTPException(
            status_code=409,
            detail="Worker busy — try again in a moment",
        ) from exc

    if items == -1:
        raise HTTPException(
            status_code=409,
            detail="A pipeline stage is actively running — retry not allowed",
        )

    dispatch_batch_retry(items, batch_fallback=batch_fallback, batch_id=batch_id, db=db)

    # OOB row update + global badges
    bundles = get_triage_bundles(db)
    bundle_key = f"batch-{batch_id}"
    updated_bundle = next((b for b in bundles if b.key == bundle_key), None)

    oob_parts: list[str] = []
    if updated_bundle:
        oob_parts.append(render_bundle_group_oob(request, updated_bundle, db))
    oob_parts.append(render_sidebar_badges_oob(db))
    oob_parts.append(render_triage_header_stats_oob(request, db, bundles=bundles))

    trigger = {"triage:bundle-retried": {"batch_id": batch_id, "doc_count": doc_count}}

    response = HTMLResponse(content="".join(oob_parts))
    response.headers["HX-Trigger"] = json.dumps(trigger)
    return response


@router.post("/triage/retry-all")
@limiter.limit("5/minute")
async def retry_all_bundles(
    request: Request,
    db: Session = Depends(get_db),
):
    """Retry the AI pipeline for every bundle currently in triage.

    Bundles with an actively running stage are skipped rather than rejected.
    Bundles that can't acquire the write lock after retries are also skipped.
    """
    from app.models.database import IngestBatch
    from app.services.pipeline_status import retry_on_db_locked

    bundles = get_triage_bundles(db, limit=500, enrich=False)
    batch_ids = {b.batch_id for b in bundles if b.batch_id is not None}

    batches = db.query(IngestBatch).filter(IngestBatch.id.in_(batch_ids)).all()

    retried = 0
    for batch in batches:

        def _do_reset(b=batch):
            res = reset_batch_for_retry(b, db, full=False)
            if res == -1:
                return -1
            its, bf = res
            db.commit()
            return its, bf

        try:
            result = retry_on_db_locked(_do_reset, db)
        except OperationalError:
            logger.warning(
                "retry-all: batch %s still locked after retries; skipping", batch.id
            )
            continue
        if result == -1:
            continue  # actively-running bundles are silently skipped
        items, batch_fallback = result
        dispatch_batch_retry(
            items, batch_fallback=batch_fallback, batch_id=batch.id, db=db
        )
        retried += 1

    oob_parts = [
        render_triage_feed_oob(request, db),
        render_sidebar_badges_oob(db),
        render_triage_header_stats_oob(request, db),
    ]

    bundle_word = "bundle" if retried == 1 else "bundles"
    trigger = {
        "triage:bundles-retried": {
            "count": retried,
            "message": f"Retried {retried} {bundle_word}",
        }
    }

    response = HTMLResponse(content="".join(oob_parts))
    response.headers["HX-Trigger"] = json.dumps(trigger)
    return response


@router.get("/triage/bundle/{batch_id}")
def get_bundle(
    request: Request,
    batch_id: int,
    db: Session = Depends(get_db),
):
    """Return the rendered HTML for a single bundle group (no OOB)."""
    bundle = get_bundle_by_batch_id(db, batch_id)
    if not bundle:
        raise HTTPException(status_code=404, detail=f"Batch {batch_id} not found")

    reactions_by_doc = get_reactions_by_doc_ids(
        db, [doc.id for doc in bundle.documents]
    )

    return templates.TemplateResponse(
        request,
        "partials/triage_row.html",
        {
            "bundle": bundle,
            "reactions_by_doc": reactions_by_doc,
            "originator_colors": ORIGINATOR_COLORS,
            "originator_icons": ORIGINATOR_ICONS,
            "ORIGINATOR_COLORS": ORIGINATOR_COLORS,
            "OriginatorType": OriginatorType,
            "UserReactionType": UserReactionType,
        },
    )


@router.get("/triage/bundle/{batch_id}/pipeline")
def bundle_pipeline_status(
    request: Request,
    batch_id: int,
    db: Session = Depends(get_db),
):
    """Return pipeline aggregate chip for a bundle (triage bundle header polling).

    Uses a focused single-table query — does not rebuild the full triage feed.
    """
    from types import SimpleNamespace

    from app.repositories.document import DocumentRepository
    from app.services.pipeline_status import (
        aggregate_pipeline_summary,
        retry_on_db_locked,
    )

    stages_per_doc = DocumentRepository(db).get_pipeline_stages_for_batch(batch_id)
    if not stages_per_doc:
        return HTMLResponse("", status_code=404)

    summary = aggregate_pipeline_summary(stages_per_doc)

    n_total = summary.get("total", 0)
    n_done = (
        summary.get("completed", 0)
        + summary.get("failed", 0)
        + summary.get("skipped", 0)
    )

    # Minimal stub — template only needs .pipeline_summary, .key, .batch_id
    bundle_stub = SimpleNamespace(
        batch_id=batch_id,
        key=f"batch-{batch_id}",
        pipeline_summary=summary,
    )

    response = templates.TemplateResponse(
        request,
        "partials/_pipeline_aggregate.html",
        {"bundle": bundle_stub},
    )

    # Bundle row re-renders on three cues so title, originator, and case
    # assignment become visible incrementally without a manual page refresh:
    #   metadata, batch_analysis, enrich each fire once when they first go
    #   terminal across the batch (latched in IngestBatch.meta["reload_fired"]).
    #   A final unconditional reload fires when every stage is terminal.
    fire_reload = n_total > 0 and n_done == n_total

    if not fire_reload and bool(stages_per_doc):
        _TERMINAL = {"completed", "failed", "skipped"}
        from app.models.database import IngestBatch

        batch = db.query(IngestBatch).filter(IngestBatch.id == batch_id).first()
        if batch is not None:
            meta = dict(batch.meta or {})
            fired = dict(meta.get("reload_fired") or {})

            for stage_key in ("metadata", "batch_analysis", "enrich"):
                if fired.get(stage_key):
                    continue
                if all(
                    (d.get(stage_key) or {}).get("status") in _TERMINAL
                    for d in stages_per_doc
                ):
                    fired[stage_key] = True
                    fire_reload = True

            if fire_reload:
                meta["reload_fired"] = fired
                batch.meta = meta
                # Celery workers write to ingest_batches concurrently, so
                # SQLite's single-writer lock can transiently lock us out.
                # The latch is idempotent — next poll retries — so brief
                # retry + skip-on-busy avoids 500s without losing correctness.
                try:
                    retry_on_db_locked(lambda: db.commit(), db)
                except OperationalError:
                    logger.debug(
                        "Final commit after triage operation still locked — continuing"
                    )

    if fire_reload:
        response.headers["HX-Trigger"] = json.dumps(
            {f"reload-bundle-{batch_id}": {"ts": time.time()}}
        )

    return response
