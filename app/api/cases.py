import dataclasses
from typing import Annotated

from fastapi import APIRouter, Depends, Form, HTTPException, Query, Request, Response
from fastapi.responses import HTMLResponse, JSONResponse, RedirectResponse
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
from app.services.ingestion.extractors import infer_court_level
from app.services.user_settings_service import (
    get_active_proceeding,
    mark_viewed,
    set_active_proceeding,
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

    # --- Resolve active view (query param wins; always defaults to graph) ---
    active_view = view if view is not None else "graph"

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


@router.patch("/{case_id}")
async def update_case(
    case_id: str,
    title: str = Form(None),
    status: CaseStatus = Form(None),
    db: Session = Depends(get_db),
):
    """Update a case and return HX-Refresh header."""
    case = db.query(Case).filter(Case.id == case_id).first()
    if not case:
        raise HTTPException(status_code=404, detail="Case not found")

    if title is not None:
        case.title = title
    if status is not None:
        case.status = status
        if status == CaseStatus.CLOSED:
            # Close all proceedings of this case
            db.query(Proceeding).filter(Proceeding.case_id == case_id).update(
                {"status": ProceedingStatus.CLOSED}
            )

    db.commit()
    return Response(headers={"HX-Refresh": "true"})


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
    import json

    from app.models.database import IngestBatch
    from app.services.case_service import get_or_create_case_from_reference
    from app.services.ingestion.extractors import extract_az_court_from_subject

    # Resolve az_court: use provided value or extract from batch subject
    batch = db.query(IngestBatch).filter(IngestBatch.id == ingest_batch_id).first()

    if not az_court and batch and batch.subject:
        az_court = extract_az_court_from_subject(batch.subject)

    case, matched_proceeding, _ = get_or_create_case_from_reference(
        db,
        internal_id=internal_id,
        az_court=az_court,
        court_name=court_name,
        batch_subject=case_title or (batch.subject if batch else None),
        is_draft=False,
    )
    # If a title was explicitly provided, override with the supplied title.
    if case_title:
        case.title = case_title[:80]

    # If the case was previously a draft, confirm it now since user clicked Anlegen.
    if case.is_draft:
        case.is_draft = False

    proceeding_id = matched_proceeding.id if matched_proceeding else None

    # Cascade to the batch and all its docs still in _TRIAGE
    reassigned_docs = []
    if batch:
        batch.case_id = internal_id
        if proceeding_id:
            batch.proceeding_id = proceeding_id
        for doc in batch.documents:
            if not doc.case_id or doc.case_id == "_TRIAGE":
                doc.case_id = internal_id
                if proceeding_id:
                    doc.proceeding_id = proceeding_id
                reassigned_docs.append(doc)

    db.commit()

    if reassigned_docs:
        from app.services.triage_service import _reset_and_reenrich

        _reset_and_reenrich(db, reassigned_docs)

    first_doc = None
    if batch and batch.documents:
        first_doc = batch.documents[0]
        db.refresh(first_doc)

    if not first_doc:
        from fastapi.responses import RedirectResponse as _Redirect

        return _Redirect(url=f"/cases/{internal_id}", status_code=303)

    cases = db.query(Case).filter(Case.id != "_TRIAGE").order_by(Case.title.asc()).all()
    ctx = build_hud_context(
        db, first_doc, mode="review", context="embedded", cases=cases
    )
    from app.config import templates as _templates

    response = _templates.TemplateResponse(request, "partials/hud/_container.html", ctx)

    from app.api.triage import _render_sidebar_badges_oob, _render_triage_status_bar_oob
    from app.services.triage_service import TriageService

    triage_service = TriageService(db)
    response.body += (
        _render_sidebar_badges_oob(db)
        + _render_triage_status_bar_oob(request, triage_service)
    ).encode("utf-8")

    response.headers["HX-Trigger"] = json.dumps(
        {"triage:advance": {"next_doc_id": first_doc.id}}
    )
    return response


@router.post("/{case_id}/confirm-draft")
async def confirm_draft_case(
    request: Request,
    case_id: str,
    db: Session = Depends(get_db),
):
    """Confirm an AI-created draft case (flip is_draft=False)."""
    import json

    from app.models.database import Document as Doc

    case = db.query(Case).filter(Case.id == case_id).first()
    if not case:
        from fastapi import HTTPException

        raise HTTPException(status_code=404, detail="Case not found")

    if case.is_draft:
        case.is_draft = False
        db.commit()

    first_doc = (
        db.query(Doc).filter(Doc.case_id == case_id).order_by(Doc.id.asc()).first()
    )
    if not first_doc:
        return HTMLResponse("", status_code=204)

    db.refresh(first_doc)
    cases = db.query(Case).filter(Case.id != "_TRIAGE").order_by(Case.title.asc()).all()
    ctx = build_hud_context(
        db, first_doc, mode="review", context="embedded", cases=cases
    )
    from app.config import templates as _templates

    response = _templates.TemplateResponse(request, "partials/hud/_container.html", ctx)

    from app.api.triage import _render_sidebar_badges_oob, _render_triage_status_bar_oob
    from app.services.triage_service import TriageService

    response.body += (
        _render_sidebar_badges_oob(db)
        + _render_triage_status_bar_oob(request, TriageService(db))
    ).encode("utf-8")

    response.headers["HX-Trigger"] = json.dumps(
        {"triage:advance": {"next_doc_id": first_doc.id}}
    )
    return response


@router.post("/{case_id}/reject-draft")
async def reject_draft_case(
    request: Request,
    case_id: str,
    db: Session = Depends(get_db),
):
    """Delete an AI-created draft case and revert its documents to _TRIAGE."""
    # We delegate to the full delete_case logic
    return await delete_case(case_id, db, request=request, is_rejection=True)


@router.delete("/{case_id}", response_model=None)
async def delete_case(
    case_id: str,
    db: Session = Depends(get_db),
    request: Request = None,
    is_rejection: bool = False,
):
    """Delete a case and revert all its documents and batches to _TRIAGE."""
    from fastapi import HTTPException

    from app.models.database import ActionItem, Claim, Entity, IngestBatch, LegalCost

    case = db.query(Case).filter(Case.id == case_id).first()
    if not case:
        raise HTTPException(status_code=404, detail="Case not found")

    if is_rejection and not case.is_draft:
        raise HTTPException(status_code=400, detail="Only draft cases can be rejected")

    # Collect affected docs before deletion
    docs = db.query(Document).filter(Document.case_id == case_id).all()

    # Revert docs and batches to _TRIAGE
    batch_ids = {d.ingest_batch_id for d in docs if d.ingest_batch_id}
    for doc in docs:
        doc.case_id = "_TRIAGE"
        doc.proceeding_id = None
        doc.needs_review = True  # Ensure they reappear in triage

    for batch_id in batch_ids:
        batch = db.query(IngestBatch).filter(IngestBatch.id == batch_id).first()
        if batch and batch.case_id == case_id:
            batch.case_id = None
            batch.proceeding_id = None

    # Explicit deletes (no cascade from Case to these tables today)
    db.query(Entity).filter(Entity.case_id == case_id).delete(synchronize_session=False)
    db.query(ActionItem).filter(ActionItem.case_id == case_id).delete(
        synchronize_session=False
    )
    db.query(LegalCost).filter(LegalCost.case_id == case_id).delete(
        synchronize_session=False
    )
    for claim in db.query(Claim).filter(Claim.case_id == case_id).all():
        db.delete(claim)  # ORM delete so ClaimEvidence cascade fires

    db.delete(case)  # cascades to Proceeding via relationship
    db.commit()

    # Re-enrich reverted docs so pipeline stages reset cleanly
    if docs:
        from app.services.triage_service import _reset_and_reenrich

        _reset_and_reenrich(db, docs)

    if is_rejection and request:
        # Render the first reverted doc in the triage HUD
        first_doc = docs[0] if docs else None
        if not first_doc:
            return HTMLResponse("", status_code=204)

        db.refresh(first_doc)
        cases = (
            db.query(Case)
            .filter(Case.id != "_TRIAGE", Case.is_draft.is_(False))
            .order_by(Case.title.asc())
            .all()
        )
        ctx = build_hud_context(
            db, first_doc, mode="review", context="embedded", cases=cases
        )
        from app.config import templates as _templates

        response = _templates.TemplateResponse(
            request, "partials/hud/_container.html", ctx
        )

        from app.api.triage import (
            _render_sidebar_badges_oob,
            _render_triage_status_bar_oob,
        )
        from app.services.triage_service import TriageService

        response.body += (
            _render_sidebar_badges_oob(db)
            + _render_triage_status_bar_oob(request, TriageService(db))
        ).encode("utf-8")
        return response

    return JSONResponse(content={"status": "success", "reverted_docs": len(docs)})


@router.post("")
async def create_case(
    case_id: str = Form(...),
    title: str = Form(...),
    jurisdiction: Jurisdiction = Form(Jurisdiction.DE),
    court_name: str | None = Form(None),
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

    # 2. Create initial Proceeding — infer level from court name; fall back to OTHER
    new_proceeding = Proceeding(
        case_id=case_id,
        court_name=court_name,
        court_level=infer_court_level(court_name) or ProceedingCourtLevel.OTHER,
        status=ProceedingStatus.ACTIVE,
    )
    db.add(new_proceeding)

    db.commit()

    # Redirect to the new case dashboard
    return RedirectResponse(url=f"/cases/{case_id}", status_code=303)
