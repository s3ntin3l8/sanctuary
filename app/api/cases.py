import dataclasses
from datetime import datetime

from fastapi import APIRouter, Depends, Request
from fastapi.responses import HTMLResponse
from sqlalchemy import func
from sqlalchemy.orm import Session

from app.config import templates
from app.dependencies import get_db
from app.helpers import render_page
from app.models.database import (
    Case,
    Document,
    DocumentRelationship,
    UserReaction,
)
from app.models.enums import ProceedingStatus
from app.services.case_dashboard_service import (
    CaseDashboardService,
    key_passages_for_template,
    neighbor_doc_ids,
    originator_color_for_doc,
    summary_bullets_from_ai_summary,
)
from app.services.case_graph_service import CaseGraphService
from app.services.case_service import CaseService
from app.services.user_settings_service import (
    get_active_proceeding,
    get_dashboard_view,
    mark_viewed,
    set_active_proceeding,
    set_dashboard_view,
)

router = APIRouter(prefix="/cases", tags=["pages"])

DEFAULT_PAGE_SIZE = 20
DORMANCY_DAYS = 90


def _compute_dormancy_alert(case, db) -> str | None:
    """Return a textual alert when an active proceeding has been silent past the threshold."""
    now = datetime.now()
    active_procs = [
        p for p in (case.proceedings or []) if p.status == ProceedingStatus.ACTIVE
    ]
    if not active_procs:
        return None

    oldest_silent_proc = None
    oldest_days = 0

    for proc in active_procs:
        last_activity = (
            db.query(func.max(Document.created_at))
            .filter(Document.proceeding_id == proc.id)
            .scalar()
        )
        if last_activity is None:
            last_activity = proc.started_at or proc.created_at
        if last_activity is None:
            continue
        days = (now - last_activity).days
        if days > DORMANCY_DAYS and days > oldest_days:
            oldest_silent_proc = proc
            oldest_days = days

    if oldest_silent_proc is None:
        return None

    court = oldest_silent_proc.court_name or "Unknown court"
    az = oldest_silent_proc.az_court or "no docket"
    return f"{court} ({az}) has had no activity for {oldest_days} days."


@router.get("")
async def case_directory(
    request: Request, page: int = 1, db: Session = Depends(get_db)
):
    from app.constants import CASE_STATUS_META

    case_service = CaseService(db)

    if page > 1:
        data = case_service.get_all_cases_directory_paginated(
            page=page, per_page=DEFAULT_PAGE_SIZE
        )
    else:
        data = case_service.get_all_cases_directory()

    case_titles = {c.id: c.title for c in data["cases"]}

    return render_page(
        request,
        "pages/case_directory.html",
        db=db,
        all_cases=data["cases"],
        active_cases=data["active_cases"],
        closed_cases=data["closed_cases"],
        case_titles=case_titles,
        stats_by_status=data["stats_by_status"],
        doc_counts=data["doc_counts"],
        deadline_counts=data["deadline_counts"],
        status_meta=CASE_STATUS_META,
        current_page=data.get("page", 1),
        total_pages=data.get("total_pages", 1),
        total=data["total"],
    )


@router.get("/{case_id}/brief")
async def case_brief_partial(
    request: Request, case_id: str, db: Session = Depends(get_db)
):
    """HTMX polling endpoint — returns the brief panel partial."""
    case = db.query(Case).filter(Case.id == case_id).first()
    if not case:
        return HTMLResponse(content="<p>Case not found</p>", status_code=404)
    return templates.TemplateResponse(
        request,
        "partials/case_brief_panel.html",
        {
            "case": case,
            "brief": case.ai_brief,
            "ai_brief_updated_at": case.ai_brief_updated_at,
        },
    )


@router.post("/{case_id}/brief/refresh")
async def case_brief_refresh(
    request: Request, case_id: str, db: Session = Depends(get_db)
):
    """Set brief to processing, enqueue refresh task, return spinner partial."""
    case = db.query(Case).filter(Case.id == case_id).first()
    if not case:
        return HTMLResponse(content="<p>Case not found</p>", status_code=404)

    case.ai_brief = {"status": "processing"}
    db.commit()

    from app.tasks.generate_case_brief import refresh_case_brief_task

    refresh_case_brief_task.delay(case_id)

    return templates.TemplateResponse(
        request,
        "partials/case_brief_panel.html",
        {
            "case": case,
            "brief": {"status": "processing"},
            "ai_brief_updated_at": None,
        },
    )


# ---------------------------------------------------------------------------
# Main dashboard page
# ---------------------------------------------------------------------------


@router.get("/{case_id}")
async def case_detail(
    request: Request,
    case_id: str,
    proceeding: int | None = None,
    view: str | None = None,
    filter: str = "significant+",
    db: Session = Depends(get_db),
):
    """Primary case dashboard — graph-first strategic view."""
    case = db.query(Case).filter(Case.id == case_id).first()
    if not case:
        response = render_page(
            request,
            "errors/404.html",
            db=db,
            message=f"Case {case_id} not found",
        )
        response.status_code = 404
        return response

    # --- Resolve active proceeding (query param wins; persist when given) ---
    if proceeding is not None:
        set_active_proceeding(case_id, proceeding, db)
        active_proceeding_id: int | None = proceeding
    else:
        active_proceeding_id = get_active_proceeding(case_id, db)

    # --- Resolve active view (query param wins; persist when given) --------
    if view is not None:
        set_dashboard_view(view, db)
        active_view = view
    else:
        active_view = get_dashboard_view(db)

    # --- Build the context -------------------------------------------------
    context = CaseDashboardService(db).build_context(
        case_id=case_id,
        active_proceeding_id=active_proceeding_id,
        active_view=active_view,
        significance_filter=filter,
    )
    if context is None:
        response = render_page(
            request,
            "errors/404.html",
            db=db,
            message=f"Case {case_id} not found",
        )
        response.status_code = 404
        return response

    response = render_page(
        request,
        "pages/case_dashboard.html",
        db=db,
        **context,
    )

    mark_viewed(case_id, db)
    db.commit()
    return response


# ---------------------------------------------------------------------------
# Graph partial (HTMX swap target for proceeding/filter changes)
# ---------------------------------------------------------------------------


@router.get("/{case_id}/graph")
async def case_graph_partial(
    request: Request,
    case_id: str,
    proceeding: int | None = None,
    filter: str = "significant+",
    db: Session = Depends(get_db),
):
    """Return just the correspondence-graph partial for HTMX swaps."""
    case = db.query(Case).filter(Case.id == case_id).first()
    if not case:
        return HTMLResponse(content="<p>Case not found</p>", status_code=404)

    if proceeding is None:
        proceeding = get_active_proceeding(case_id, db)

    if proceeding is None:
        # No proceeding to graph — return an empty placeholder fragment.
        return templates.TemplateResponse(
            request,
            "partials/dashboard/correspondence_graph.html",
            {
                "graph": _empty_graph_dict(filter),
                "case": case,
            },
        )

    payload = CaseGraphService(db).build_payload(proceeding, filter)
    graph_dict = dataclasses.asdict(payload)

    return templates.TemplateResponse(
        request,
        "partials/dashboard/correspondence_graph.html",
        {
            "graph": graph_dict,
            "case": case,
        },
    )


def _empty_graph_dict(filter_mode: str) -> dict:
    from app.services.case_graph_service import LANE_W, LANES, LEFT, TOP

    return {
        "lanes": list(LANES),
        "nodes": [],
        "bundles": [],
        "edges": [],
        "proof_badges": {},
        "svg_width": LEFT * 2 + len(LANES) * LANE_W,
        "svg_height": TOP + 120,
        "hidden_counts": {"administrative": 0, "informational": 0},
        "filter": filter_mode,
        "node_count": 0,
        "edge_count": 0,
    }


# ---------------------------------------------------------------------------
# HUD partial — document slide-in inside the dashboard
# ---------------------------------------------------------------------------


@router.get("/{case_id}/document/{doc_id}/hud")
async def case_document_hud(
    request: Request,
    case_id: str,
    doc_id: int,
    db: Session = Depends(get_db),
):
    """Render the document HUD slide-in for the given doc within the case."""
    doc = db.query(Document).filter(Document.id == doc_id).first()
    if not doc or doc.case_id != case_id:
        return HTMLResponse(content="<p>Document not found</p>", status_code=404)

    # User reactions (first-class triage data recalled by AI)
    reactions = (
        db.query(UserReaction)
        .filter(UserReaction.document_id == doc.id)
        .order_by(UserReaction.created_at.asc())
        .all()
    )

    # Document relationships — incoming + outgoing
    rels_out = (
        db.query(DocumentRelationship)
        .filter(DocumentRelationship.from_document_id == doc.id)
        .all()
    )
    rels_in = (
        db.query(DocumentRelationship)
        .filter(DocumentRelationship.to_document_id == doc.id)
        .all()
    )

    # Titles for the related documents
    related_ids = {r.to_document_id for r in rels_out} | {
        r.from_document_id for r in rels_in
    }
    titles_by_id: dict[int, str] = {}
    if related_ids:
        for row in (
            db.query(Document.id, Document.title)
            .filter(Document.id.in_(related_ids))
            .all()
        ):
            titles_by_id[row[0]] = row[1] or "Untitled"

    def _rel_list(rels, *, side: str) -> list[dict]:
        out = []
        for rel in rels:
            other_id = rel.to_document_id if side == "out" else rel.from_document_id
            out.append(
                {
                    "id": other_id,
                    "title": titles_by_id.get(other_id, "Untitled"),
                    "rel_type": rel.relationship_type.value
                    if rel.relationship_type
                    else "related",
                }
            )
        return out

    relationships_in = _rel_list(rels_in, side="in")
    relationships_out = _rel_list(rels_out, side="out")

    summary_bullets = summary_bullets_from_ai_summary(doc.ai_summary)
    key_passages = key_passages_for_template(doc.key_passages)
    prev_doc_id, next_doc_id = neighbor_doc_ids(db, doc)
    originator_color = originator_color_for_doc(doc)

    return templates.TemplateResponse(
        request,
        "partials/dashboard/case_dashboard_hud.html",
        {
            "doc": doc,
            "case_id": case_id,
            "summary_bullets": summary_bullets,
            "key_passages": key_passages,
            "reactions": reactions,
            "relationships_in": relationships_in,
            "relationships_out": relationships_out,
            "prev_doc_id": prev_doc_id,
            "next_doc_id": next_doc_id,
            "originator_color": originator_color,
        },
    )
