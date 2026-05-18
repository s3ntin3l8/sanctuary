"""Triage feed: read-only page renders (triage page, feed partial, card live)."""

from __future__ import annotations

from fastapi import APIRouter, Depends, Query, Request
from fastapi.responses import HTMLResponse
from sqlalchemy.orm import Session

from app.config import templates
from app.constants import ORIGINATOR_COLORS, ORIGINATOR_ICONS
from app.dependencies import get_db
from app.helpers import render_page
from app.models.database import Case, Document
from app.models.enums import OriginatorType, UserReactionType
from app.repositories.case import CaseRepository
from app.services.triage_bundles import (
    get_slicing_queue,
    get_triage_bundles,
    get_triage_filter_options,
)
from app.services.triage_oob_render import render_row_targeted_oob
from app.services.triage_reactions import get_reactions_by_doc_ids
from app.services.triage_view import failed_doc_summary

router = APIRouter()


@router.get("/triage")
def triage_page(
    request: Request,
    limit: int = 50,
    offset: int = 0,
    sort: str = "received",
    dir: str = "desc",
    case_id: list[str] = Query(default=[]),
    proceeding_id: list[str] = Query(default=[]),
    pipeline_filter: list[str] = Query(default=[]),
    db: Session = Depends(get_db),
):
    from app.models.database import Proceeding

    filter_options = get_triage_filter_options(db)

    bundles = get_triage_bundles(
        db,
        limit=limit,
        offset=offset,
        sort=sort,
        direction=dir,
        case_ids=case_id,
        proceeding_ids=proceeding_id,
        pipeline_filters=pipeline_filter,
    )
    slicing_queue = get_slicing_queue(db)
    all_cases = CaseRepository(db).list_for_picker()
    total_docs = sum(b.doc_count for b in bundles)

    all_doc_ids = [doc.id for bundle in bundles for doc in bundle.documents]
    reactions_by_doc = get_reactions_by_doc_ids(db, all_doc_ids)

    proceedings = db.query(Proceeding).order_by(Proceeding.court_name.asc()).all()

    drafts_pending = db.query(Case).filter(Case.is_draft.is_(True)).count()
    first_draft_doc_id = None
    if drafts_pending:
        _row = (
            db.query(Document.id)
            .join(Case, Case.id == Document.case_id)
            .filter(Case.is_draft.is_(True))
            .order_by(Document.id.asc())
            .first()
        )
        if _row:
            first_draft_doc_id = _row[0]

    failed_count, first_failed_doc_id = failed_doc_summary(bundles)

    from app.services.triage_view import stats_for_chips

    header_stats = stats_for_chips(bundles)
    sub_bundles_by_key = {b.key: b.sub_bundles for b in bundles}
    mock_status_by_key = {b.key: b.mock_status for b in bundles}

    return render_page(
        request,
        "pages/triage.html",
        db=db,
        bundles=bundles,
        slicing_queue=slicing_queue,
        all_cases=all_cases,
        cases=all_cases,
        proceedings=proceedings,
        total_docs=total_docs,
        drafts_pending=drafts_pending,
        first_draft_doc_id=first_draft_doc_id,
        failed_count=failed_count,
        first_failed_doc_id=first_failed_doc_id,
        reactions_by_doc=reactions_by_doc,
        header_stats=header_stats,
        sub_bundles_by_key=sub_bundles_by_key,
        mock_status_by_key=mock_status_by_key,
        limit=limit,
        offset=offset,
        sort=sort,
        dir=dir,
        case_ids=case_id,
        proceeding_ids=proceeding_id,
        pipeline_filters=pipeline_filter,
        case_options=filter_options["case_options"],
        proceeding_options=filter_options["proceeding_options"],
        pipeline_options=filter_options["pipeline_options"],
        originator_colors=ORIGINATOR_COLORS,
        originator_icons=ORIGINATOR_ICONS,
        OriginatorType=OriginatorType,
        UserReactionType=UserReactionType,
    )


@router.get("/triage/feed")
def triage_feed_partial(
    request: Request,
    limit: int = 50,
    offset: int = 0,
    sort: str = "received",
    dir: str = "desc",
    case_id: list[str] = Query(default=[]),
    proceeding_id: list[str] = Query(default=[]),
    pipeline_filter: list[str] = Query(default=[]),
    db: Session = Depends(get_db),
):
    from app.services.triage_view import stats_for_chips

    bundles = get_triage_bundles(
        db,
        limit=limit,
        offset=offset,
        sort=sort,
        direction=dir,
        case_ids=case_id,
        proceeding_ids=proceeding_id,
        pipeline_filters=pipeline_filter,
    )
    all_doc_ids = [doc.id for bundle in bundles for doc in bundle.documents]
    reactions_by_doc = get_reactions_by_doc_ids(db, all_doc_ids)
    header_stats = stats_for_chips(bundles)
    sub_bundles_by_key = {b.key: b.sub_bundles for b in bundles}
    mock_status_by_key = {b.key: b.mock_status for b in bundles}

    feed_html = templates.get_template("partials/triage_feed.html").render(
        {
            "request": request,
            "bundles": bundles,
            "case_ids": case_id,
            "proceeding_ids": proceeding_id,
            "pipeline_filters": pipeline_filter,
            "reactions_by_doc": reactions_by_doc,
            "sub_bundles_by_key": sub_bundles_by_key,
            "mock_status_by_key": mock_status_by_key,
            "originator_colors": ORIGINATOR_COLORS,
            "originator_icons": ORIGINATOR_ICONS,
            "OriginatorType": OriginatorType,
            "UserReactionType": UserReactionType,
        }
    )

    stats_html = templates.get_template("partials/triage_filter_chips.html").render(
        {
            "request": request,
            "header_stats": header_stats,
            "as_oob": True,
        }
    )

    return HTMLResponse(content=feed_html + stats_html)


@router.get("/triage/card/{doc_id}/live")
def triage_card_live(
    request: Request,
    doc_id: int,
    db: Session = Depends(get_db),
):
    """Return OOB row swap for a single doc (polling refresh).

    The row aggregates the doc's bundle; the new triage row template owns its own
    polling probe scoped per bundle, but this endpoint is still used by direct
    consumers (e.g., chunked retries that target a single doc).
    """
    doc = db.query(Document).filter(Document.id == doc_id).first()
    if not doc:
        return HTMLResponse("", status_code=404)

    return HTMLResponse(render_row_targeted_oob(request, doc, db, allow_delete=False))
