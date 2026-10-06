import re
from typing import Annotated

from fastapi import APIRouter, Depends, Form, HTTPException, Query, Request, Response
from fastapi.responses import HTMLResponse, JSONResponse, RedirectResponse
from pydantic import BaseModel
from sqlalchemy.orm import Session, joinedload

from app.api.access_guards import require_case_access
from app.config import templates
from app.core.rate_limit import limiter
from app.dependencies import get_current_user, get_db
from app.helpers import render_page
from app.models.database import (
    Case,
    Document,
    Proceeding,
    User,
)
from app.models.enums import (
    CaseStatus,
    CaseType,
    Jurisdiction,
    ProceedingStatus,
)
from app.services.case_dashboard_service import CaseDashboardService
from app.services.case_service import CaseIdTaken, CaseService
from app.services.hud_context import build_hud_context
from app.services.user_settings_service import (
    get_active_proceeding,
    mark_viewed,
    set_active_proceeding,
)
from app.spa import spa_index

router = APIRouter(prefix="/cases", tags=["pages"])

FilterQuery = Annotated[str, Query(pattern=r"^(critical|significant\+|all)$")]


@router.get("", include_in_schema=False)
def case_directory():
    """The cases directory is an SPA route (data comes from /api/v1/cases)."""
    return spa_index()


@router.get("/{case_id}/brief")
async def case_brief_partial(
    request: Request,
    case_id: str,
    db: Session = Depends(get_db),
    user: User = Depends(get_current_user),
):
    """HTMX polling endpoint — returns the brief panel partial."""
    from app.services import access_service

    case = db.query(Case).filter(Case.id == case_id).first()
    if not case or not access_service.can_view_case(db, user, case):
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
@limiter.limit("10/minute")
async def case_brief_refresh(
    request: Request,
    case_id: str,
    db: Session = Depends(get_db),
    case: Case = Depends(require_case_access(edit=True)),
):
    """Set brief to processing, enqueue refresh task, return spinner partial."""
    case.ai_brief = {"status": "processing"}
    db.commit()

    from app.tasks.dispatch import dispatch_task
    from app.tasks.generate_case_brief import refresh_case_brief_task

    dispatch_task(refresh_case_brief_task, case_id)

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
    user: User = Depends(get_current_user),
):
    """Primary case dashboard — graph-first strategic view."""
    from app.services import access_service

    case = db.query(Case).filter(Case.id == case_id).first()
    if not case or not access_service.can_view_case(db, user, case):
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
        set_active_proceeding(case_id, proceeding, db, user.id)
        active_proceeding_id: int | None = proceeding
    else:
        active_proceeding_id = get_active_proceeding(case_id, db, user.id)

    # --- Resolve active view (query param wins; always defaults to graph) ---
    active_view = view if view is not None else "graph"

    # --- Build the context -------------------------------------------------
    context = CaseDashboardService(db).build_context(
        case_id=case_id,
        active_proceeding_id=active_proceeding_id,
        active_view=active_view,
        significance_filter=filter,
        user_id=user.id,
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

    mark_viewed(case_id, db, user.id)
    db.commit()
    return response


@router.patch("/{case_id}")
async def update_case(
    case_id: str,
    title: str = Form(None),
    status: CaseStatus = Form(None),
    case_type: CaseType = Form(None),
    assume_worst_case: bool = Form(None),
    db: Session = Depends(get_db),
    case: Case = Depends(require_case_access(edit=True)),
):
    """Update a case and return HX-Refresh header."""
    if title is not None:
        if not title.strip():
            raise HTTPException(status_code=422, detail="Case title cannot be empty")
        case.title = title.strip()
    if status is not None:
        case.status = status
        if status == CaseStatus.CLOSED:
            db.query(Proceeding).filter(Proceeding.case_id == case_id).update(
                {"status": ProceedingStatus.CLOSED}
            )
    if case_type is not None:
        case.case_type = case_type
    if assume_worst_case is not None:
        case.assume_worst_case = assume_worst_case

    db.commit()
    return Response(headers={"HX-Refresh": "true"})


# ---------------------------------------------------------------------------
# HUD partial — document slide-in inside the dashboard
# ---------------------------------------------------------------------------


@router.get("/{case_id}/document/{doc_id}/hud")
async def case_document_hud(
    request: Request,
    case_id: str,
    doc_id: int,
    db: Session = Depends(get_db),
    user: User = Depends(get_current_user),
):
    """Render the document HUD slide-in for the given doc within the case."""
    from app.services import access_service

    doc = db.query(Document).filter(Document.id == doc_id).first()
    if not doc or doc.case_id != case_id:
        return HTMLResponse(content="<p>Document not found</p>", status_code=404)
    case = db.query(Case).filter(Case.id == case_id).first()
    if not access_service.can_view_case(db, user, case):
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
    user: User = Depends(get_current_user),
):
    """Full-screen document reader at /cases/:case_id/document/:doc_id."""
    from app.helpers import render_page
    from app.services import access_service

    doc = (
        db.query(Document)
        .options(
            joinedload(Document.proceeding),
            joinedload(Document.children),
        )
        .filter(Document.id == doc_id)
        .first()
    )
    case = db.query(Case).filter(Case.id == case_id).first()
    if (
        not doc
        or doc.case_id != case_id
        or not access_service.can_view_case(db, user, case)
    ):
        from fastapi.responses import RedirectResponse

        return RedirectResponse(f"/cases/{case_id}", status_code=302)

    ctx = build_hud_context(db, doc, mode="read")
    ctx["context"] = "standalone"
    ctx["case_id"] = case_id
    return render_page(request, "pages/document.html", db=db, **ctx)


@router.post("/{case_id}/confirm-draft")
async def confirm_draft_case(
    request: Request,
    case_id: str,
    db: Session = Depends(get_db),
    case: Case = Depends(require_case_access(edit=True)),
):
    """Confirm an AI-created draft case (flip is_draft=False)."""
    import json

    from app.models.database import Document as Doc

    if case.is_draft:
        case.is_draft = False
        db.commit()

    first_doc = (
        db.query(Doc).filter(Doc.case_id == case_id).order_by(Doc.id.asc()).first()
    )
    if not first_doc:
        return HTMLResponse("", status_code=204)

    db.refresh(first_doc)
    ctx = build_hud_context(db, first_doc, mode="read", context="embedded")
    from app.config import templates as _templates

    response = _templates.TemplateResponse(request, "partials/hud/_container.html", ctx)
    case_doc_count = db.query(Document).filter(Document.case_id == case_id).count()
    response.headers["HX-Trigger"] = json.dumps(
        {
            "triage:advance": {"next_doc_id": first_doc.id},
            "case:confirmed": {
                "case_id": case.id,
                "case_title": case.title,
                "doc_count": case_doc_count,
                "action": "ratified",
            },
        }
    )
    return response


def _delete_case_via_service(case_id: str, db: Session) -> dict:
    """Shared between DELETE /cases/:id and POST /cases/:id/reject-draft."""
    from fastapi import HTTPException

    if not db.query(Case).filter(Case.id == case_id).first():
        raise HTTPException(status_code=404, detail="Case not found")

    try:
        result = CaseService(db).delete_and_revert(case_id)
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e)) from e
    if result is None:
        raise HTTPException(status_code=404, detail="Case not found")
    return result


@router.post("/{case_id}/reject-draft")
async def reject_draft_case(
    request: Request,
    case_id: str,
    db: Session = Depends(get_db),
    case: Case = Depends(require_case_access(edit=True)),
):
    """Delete an AI-created draft case and revert its documents to _TRIAGE."""
    import json

    if not case.is_draft:
        raise HTTPException(status_code=400, detail="Only draft cases can be rejected")

    result = _delete_case_via_service(case_id, db)
    docs = result["docs"]

    first_doc = docs[0] if docs else None
    if not first_doc:
        return HTMLResponse("", status_code=204)

    db.refresh(first_doc)
    ctx = build_hud_context(db, first_doc, mode="read", context="embedded")
    from app.config import templates as _templates

    response = _templates.TemplateResponse(request, "partials/hud/_container.html", ctx)

    response.headers["HX-Trigger"] = json.dumps(
        {"case:rejected": {"case_id": case_id, "doc_count": result["doc_count"]}}
    )
    return response


@router.delete("/{case_id}", response_model=None)
async def delete_case(
    case_id: str,
    db: Session = Depends(get_db),
    case: Case = Depends(require_case_access(edit=True)),
):
    """Delete a case and revert all its documents and batches to _TRIAGE."""
    result = _delete_case_via_service(case_id, db)
    return JSONResponse(
        content={"status": "success", "reverted_docs": result["doc_count"]}
    )


class PurgeConfirm(BaseModel):
    confirm: str  # must equal f"purge {case_id}"


@router.delete("/{case_id}/purge")
@limiter.limit("5/minute")
def purge_case(
    case_id: str,
    body: PurgeConfirm,
    request: Request,
    db: Session = Depends(get_db),
    case: Case = Depends(require_case_access(edit=True)),
):
    """Hard-delete a case and erase its on-disk data directory."""
    expected = f"purge {case_id}"
    if body.confirm != expected:
        raise HTTPException(
            status_code=400, detail=f"confirm must be exactly '{expected}'"
        )
    service = CaseService(db)
    try:
        result = service.purge(case_id)
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e)) from e
    if result is None:
        raise HTTPException(status_code=404, detail="Case not found")
    return result


@router.post("")
async def create_case(
    case_id: str = Form(...),
    title: str = Form(...),
    jurisdiction: Jurisdiction = Form(Jurisdiction.DE),
    # Required: proceedings.court_name is NOT NULL and the create-case form
    # (create_case_modal.html) always sends it. This was previously typed as
    # optional, which let a request without it slip past FastAPI's own
    # validation into an unhandled DB IntegrityError (500) instead of a
    # clean 422.
    court_name: str = Form(...),
    db: Session = Depends(get_db),
    user: User = Depends(get_current_user),
):
    """Create a new case and its initial active proceeding."""
    case_id = case_id.strip()
    if not re.fullmatch(r"[A-Za-z0-9._-]+", case_id):
        raise HTTPException(status_code=422, detail="Case id must be URL-safe.")
    try:
        CaseService(db).create_case_with_proceeding(
            case_id=case_id,
            title=title.strip(),
            court_name=court_name.strip(),
            jurisdiction=jurisdiction,
            owner_id=user.id,
        )
    except CaseIdTaken as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc

    # Redirect to the new case dashboard
    return RedirectResponse(url=f"/cases/{case_id}", status_code=303)


@router.post("/{case_id}/opposing-parties")
async def save_opposing_parties(
    request: Request,
    case_id: str,
    opposing_parties: str = Form(""),
    db: Session = Depends(get_db),
    case: Case = Depends(require_case_access(edit=True)),
):
    """Save the per-case opposing party list."""
    from app.services.case_service import set_case_opposing_parties

    parties = [p.strip() for p in opposing_parties.split(",") if p.strip()]
    set_case_opposing_parties(case_id, parties, db)
    db.commit()
    db.refresh(case)

    return templates.TemplateResponse(
        request,
        "partials/case_parties_panel.html",
        {
            "request": request,
            "parties": case.parties or [],
            "case": case,
            "opposing_parties_raw": ", ".join(case.opposing_parties or []),
            "saved": True,
        },
    )


@router.post("/{case_id}/reenrich")
@limiter.limit("5/minute")
async def reenrich_case(
    request: Request,
    case_id: str,
    db: Session = Depends(get_db),
    case: Case = Depends(require_case_access(edit=True)),
):
    """Queue all documents in a case for re-enrichment using the current party identity."""
    from app.services.triage_confirmation import reset_and_reenrich

    docs = db.query(Document).filter(Document.case_id == case_id).all()
    if docs:
        reset_and_reenrich(db, docs)

    return Response(
        content=f'<span class="text-xs text-primary font-bold">{len(docs)} documents queued for re-enrichment</span>',
        media_type="text/html",
    )
