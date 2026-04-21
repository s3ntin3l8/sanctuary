import dataclasses
from typing import Annotated

from fastapi import APIRouter, Depends, Form, Query, Request
from fastapi.responses import HTMLResponse, RedirectResponse
from sqlalchemy.orm import Session, joinedload

from app.config import templates
from app.dependencies import get_db
from app.helpers import render_page
from app.models.database import (
    Case,
    Document,
    Proceeding,
)
from app.models.enums import (
    CaseStatus,
    Jurisdiction,
    ProceedingCourtLevel,
    ProceedingStatus,
)
from app.services.case_dashboard_service import CaseDashboardService
from app.services.case_graph_service import CaseGraphService
from app.services.case_service import (  # noqa: F401 (re-exported for tests)
    DORMANCY_DAYS,
    CaseService,
    _compute_dormancy_alert,
)
from app.services.hud_context import build_hud_context
from app.services.user_settings_service import (
    get_active_proceeding,
    get_dashboard_view,
    mark_viewed,
    set_active_proceeding,
    set_dashboard_view,
)

router = APIRouter(prefix="/cases", tags=["pages"])

DEFAULT_PAGE_SIZE = 20

FilterQuery = Annotated[str, Query(pattern=r"^(critical|significant\+|all)$")]


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
    filter: FilterQuery = "significant+",
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
    filter: FilterQuery = "significant+",
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

    ctx = build_hud_context(db, doc, mode="read")
    ctx["context"] = "overlay"
    ctx["case_id"] = case_id
    return templates.TemplateResponse(request, "partials/hud/_container.html", ctx)


@router.get("/{case_id}/document/{doc_id}")
async def case_document_fullscreen(
    request: Request,
    case_id: str,
    doc_id: int,
    db: Session = Depends(get_db),
):
    """Full-screen document reader at /cases/:case_id/document/:doc_id."""
    from app.helpers import render_page

    doc = (
        db.query(Document)
        .options(
            joinedload(Document.proceeding),
            joinedload(Document.children),
        )
        .filter(Document.id == doc_id)
        .first()
    )
    if not doc or doc.case_id != case_id:
        from fastapi.responses import RedirectResponse

        return RedirectResponse(f"/cases/{case_id}", status_code=302)

    ctx = build_hud_context(db, doc, mode="read")
    ctx["context"] = "standalone"
    ctx["case_id"] = case_id
    return render_page(request, "pages/document.html", db=db, **ctx)


@router.post("")
async def create_case(
    case_id: str = Form(...),
    title: str = Form(...),
    jurisdiction: Jurisdiction = Form(Jurisdiction.DE),
    court_name: str = Form(...),
    db: Session = Depends(get_db),
):
    """Create a new case and its initial active proceeding."""
    # 1. Create the Case
    new_case = Case(
        id=case_id,
        title=title,
        status=CaseStatus.INTAKE,
        jurisdiction=jurisdiction,
    )
    db.add(new_case)

    # 2. Create initial Proceeding
    # We default to local court (Amtsgericht) for new intake cases
    new_proceeding = Proceeding(
        case_id=case_id,
        court_name=court_name,
        court_level=ProceedingCourtLevel.AG,
        status=ProceedingStatus.ACTIVE,
    )
    db.add(new_proceeding)

    db.commit()

    # Redirect to the new case dashboard
    return RedirectResponse(url=f"/cases/{case_id}", status_code=303)
