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
    from datetime import datetime

    from app.constants import CASE_STATUS_META

    case_service = CaseService(db)

    if page > 1:
        data = case_service.get_all_cases_directory_paginated(
            page=page, per_page=DEFAULT_PAGE_SIZE
        )
    else:
        data = case_service.get_all_cases_directory()

    case_titles = {c["id"]: c["title"] for c in data["cases"]}
    now = datetime.now()

    return render_page(
        request,
        "pages/case_directory.html",
        db=db,
        now=now,
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
                "active_proceeding": None,
            },
        )

    # Fetch reaction_map to ensure titles are clipped correctly when reactions are present
    dash_service = CaseDashboardService(db)
    reaction_map = dash_service._reaction_map_for_proceeding(proceeding)

    payload = CaseGraphService(db).build_payload(
        proceeding, filter, reaction_map=reaction_map
    )
    graph_dict = dataclasses.asdict(payload)

    return templates.TemplateResponse(
        request,
        "partials/dashboard/correspondence_graph.html",
        {
            "graph": graph_dict,
            "case": case,
            "active_proceeding": db.get(Proceeding, proceeding),
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
        "node_counts": {
            "critical": 0,
            "significant": 0,
            "informational": 0,
            "administrative_standalone": 0,
            "administrative_relay": 0,
        },
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


@router.post("/create-from-triage")
async def create_case_from_triage(
    request: Request,
    internal_id: str = Form(...),
    ingest_batch_id: int = Form(...),
    az_court: str | None = Form(None),
    court_name: str | None = Form(None),
    case_title: str | None = Form(None),
    db: Session = Depends(get_db),
):
    """Create a new case from the triage metadata form and cascade to the full batch."""
    from app.models.database import IngestBatch
    from app.models.enums import OriginatorType
    from app.services.hud_context import build_triage_hud_context
    from app.services.ingestion.extractors import extract_az_court_from_subject

    # Derive case title from batch subject when not provided
    batch = db.query(IngestBatch).filter(IngestBatch.id == ingest_batch_id).first()
    if batch and not case_title and batch.subject:
        subj = batch.subject
        # Strip leading internal_id token (e.g. "8372/25 - " or "8372/25: ")
        stripped = subj.lstrip()
        if stripped.startswith(internal_id):
            remainder = stripped[len(internal_id) :].lstrip(" -:/")
        else:
            remainder = subj
        # Trim at common German subject separators
        for sep in (" vor dem ", " wg. ", " bzgl. ", " betr. "):
            idx = remainder.lower().find(sep)
            if idx != -1:
                remainder = remainder[:idx]
        case_title = remainder.strip()[:80] or None

    # Resolve az_court: use provided value or extract from batch subject
    if not az_court and batch and batch.subject:
        az_court = extract_az_court_from_subject(batch.subject)

    # Create or retrieve the case
    existing_case = db.query(Case).filter(Case.id == internal_id).first()
    if not existing_case:
        new_case = Case(
            id=internal_id,
            title=case_title or f"Neuer Fall {internal_id}",
            status=CaseStatus.INTAKE,
            jurisdiction=Jurisdiction.DE,
        )
        db.add(new_case)
        db.flush()

    # Create a Proceeding if we have an az_court
    matched_proceeding = None
    if az_court:
        matched_proceeding = (
            db.query(Proceeding)
            .filter(Proceeding.case_id == internal_id, Proceeding.az_court == az_court)
            .first()
        )
        if not matched_proceeding:
            matched_proceeding = Proceeding(
                case_id=internal_id,
                az_court=az_court,
                court_name=court_name or "(Gericht folgt)",
                court_level=ProceedingCourtLevel.AG,
                status=ProceedingStatus.ACTIVE,
            )
            db.add(matched_proceeding)
            db.flush()

    proceeding_id = matched_proceeding.id if matched_proceeding else None

    # Cascade to the batch and all its docs still in _TRIAGE
    if batch:
        batch.case_id = internal_id
        if proceeding_id:
            batch.proceeding_id = proceeding_id
        for doc in batch.documents:
            if not doc.case_id or doc.case_id == "_TRIAGE":
                doc.case_id = internal_id
                if proceeding_id:
                    doc.proceeding_id = proceeding_id

    db.commit()

    # Identify the first doc in this batch to render in the HUD
    first_doc = None
    if batch and batch.documents:
        first_doc = batch.documents[0]
        db.refresh(first_doc)

    if not first_doc:
        from fastapi.responses import RedirectResponse as _Redirect

        return _Redirect(url=f"/cases/{internal_id}", status_code=303)

    cases = db.query(Case).filter(Case.id != "_TRIAGE").order_by(Case.title.asc()).all()
    ctx = build_triage_hud_context(
        db, first_doc, cases=cases, OriginatorType=OriginatorType
    )
    from app.config import templates as _templates

    response = _templates.TemplateResponse(request, "partials/hud/_container.html", ctx)

    # OOB: update sidebar badges + status bar
    from app.api.triage import _render_sidebar_badges_oob
    from app.services.triage_service import TriageService

    triage_service = TriageService(db)
    from app.api.triage import _render_triage_status_bar_oob

    response.body += (
        _render_sidebar_badges_oob(db)
        + _render_triage_status_bar_oob(request, triage_service)
    ).encode("utf-8")

    import json

    response.headers["HX-Trigger"] = json.dumps(
        {"triage:advance": {"next_doc_id": first_doc.id}}
    )
    return response


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
