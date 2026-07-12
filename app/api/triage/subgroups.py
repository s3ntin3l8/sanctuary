"""Bundle sub-group management: cover letter, create/rename/delete group, reorder, reset."""

from __future__ import annotations

from fastapi import APIRouter, Depends, Form, HTTPException, Request
from fastapi.responses import HTMLResponse
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from app.config import templates
from app.dependencies import get_db
from app.services.triage_bundles import get_bundle_by_batch_id
from app.services.triage_subgroups import (
    create_sub_group,
    delete_sub_group,
    rename_sub_group,
    reorder_documents,
    reset_sub_groups,
    set_cover_letter,
)

router = APIRouter()


def _render_picker(request: Request, batch_id: int, db: Session) -> str:
    """Re-fetch bundle and render triage_doc_tree_picker.html for HTMX outerHTML swap."""
    bundle = get_bundle_by_batch_id(db, batch_id)
    if not bundle:
        return "<div>Bundle not found</div>"

    return templates.get_template("partials/triage_doc_tree_picker.html").render(
        {
            "request": request,
            "bundle": bundle,
            "active_doc_id": None,
            "compact": True,
        }
    )


@router.post("/triage/bundle/{batch_id}/set-cover")
def triage_set_cover_letter(
    batch_id: int,
    request: Request,
    doc_id: int = Form(...),
    db: Session = Depends(get_db),
):
    """Mark a document as cover letter of its sub-group."""
    try:
        set_cover_letter(db, doc_id=doc_id, batch_id=batch_id)
    except ValueError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc
    db.commit()
    return HTMLResponse(content=_render_picker(request, batch_id, db))


@router.post("/triage/bundle/{batch_id}/new-group")
def triage_create_sub_group(
    batch_id: int,
    request: Request,
    db: Session = Depends(get_db),
):
    """Create a new empty sub-group at the end of this batch's group list."""
    try:
        create_sub_group(db, batch_id=batch_id)
        db.commit()
    except IntegrityError as exc:
        db.rollback()
        raise HTTPException(status_code=422, detail="Batch not found") from exc
    return HTMLResponse(content=_render_picker(request, batch_id, db))


@router.post("/triage/bundle/{batch_id}/rename-group")
def triage_rename_sub_group(
    batch_id: int,
    request: Request,
    sub_group_id: str = Form(""),
    lead_doc_id: str = Form(""),
    label: str = Form(""),
    db: Session = Depends(get_db),
):
    """Rename a sub-group label. Empty label clears to auto-derived.

    sub_group_id may be empty when the bundle is still in auto mode; in that
    case lead_doc_id is used to identify the group after lazy init.
    """
    sub_group_id_int = int(sub_group_id) if sub_group_id.strip() else None
    lead_doc_id_int = int(lead_doc_id) if lead_doc_id.strip() else None
    try:
        rename_sub_group(
            db,
            sub_group_id=sub_group_id_int,
            batch_id=batch_id,
            label=label,
            lead_doc_id=lead_doc_id_int,
        )
    except ValueError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc
    db.commit()
    return HTMLResponse(content=_render_picker(request, batch_id, db))


@router.post("/triage/bundle/{batch_id}/delete-group")
def triage_delete_sub_group(
    batch_id: int,
    request: Request,
    sub_group_id: str = Form(""),
    lead_doc_id: str = Form(""),
    db: Session = Depends(get_db),
):
    """Delete a sub-group. Docs reassign to the next remaining group, or
    revert to auto mode if this was the only sub-group.

    sub_group_id may be empty when the bundle is still in auto mode; in that
    case lead_doc_id is used to identify the group after lazy init.
    """
    sub_group_id_int = int(sub_group_id) if sub_group_id.strip() else None
    lead_doc_id_int = int(lead_doc_id) if lead_doc_id.strip() else None
    try:
        delete_sub_group(
            db,
            sub_group_id=sub_group_id_int,
            batch_id=batch_id,
            lead_doc_id=lead_doc_id_int,
        )
    except ValueError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc
    db.commit()
    return HTMLResponse(content=_render_picker(request, batch_id, db))


@router.post("/triage/bundle/{batch_id}/reorder")
def triage_reorder_documents(
    batch_id: int,
    request: Request,
    sub_group_id: str = Form(""),
    lead_doc_id: str = Form(""),
    doc_ids: str = Form(...),
    db: Session = Depends(get_db),
):
    """Update document ordering and sub-group membership after drag-drop.

    Frontend sends one POST per affected sub-group with its full ordered doc list.
    doc_ids is a comma-separated string of integer doc ids.
    sub_group_id may be empty when the bundle is still in auto mode; in that
    case lead_doc_id is used to identify the group after lazy init.
    """
    sub_group_id_int = int(sub_group_id) if sub_group_id.strip() else None
    lead_doc_id_int = int(lead_doc_id) if lead_doc_id.strip() else None
    ordered_ids = [int(x) for x in doc_ids.split(",") if x.strip()]
    reorder_documents(
        db,
        batch_id=batch_id,
        ordered_doc_ids=ordered_ids,
        target_sub_group_id=sub_group_id_int,
        lead_doc_id=lead_doc_id_int,
    )
    db.commit()
    return HTMLResponse(content=_render_picker(request, batch_id, db))


@router.post("/triage/bundle/{batch_id}/reset-groups")
def triage_reset_sub_groups(
    batch_id: int,
    request: Request,
    db: Session = Depends(get_db),
):
    """Remove manual sub-groups, reverting this batch to auto-grouped mode."""
    reset_sub_groups(db, batch_id)
    db.commit()
    return HTMLResponse(content=_render_picker(request, batch_id, db))
