"""Case sharing (Phase 3): owners/admins grant viewer/editor access to others."""

import json
import logging

from fastapi import APIRouter, Depends, Form, HTTPException, Request
from fastapi.responses import HTMLResponse, Response
from sqlalchemy.orm import Session

from app import helpers
from app.dependencies import get_current_user, get_db
from app.models.database import Case, CaseShare, User
from app.models.enums import CaseAccessLevel
from app.services import access_service, auth_service

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/cases", tags=["sharing"])


def _toast(message: str, kind: str = "success", status: int = 204) -> Response:
    resp = Response(status_code=status)
    resp.headers["HX-Trigger"] = json.dumps(helpers.toast_trigger(message, kind))
    return resp


def _refresh(message: str, kind: str = "success") -> Response:
    resp = Response(status_code=204)
    resp.headers["HX-Trigger"] = json.dumps(helpers.toast_trigger(message, kind))
    resp.headers["HX-Refresh"] = "true"
    return resp


def _require_owner_or_admin(db: Session, user: User, case_id: str) -> Case:
    """Only the case owner or an admin may manage shares."""
    case = db.query(Case).filter(Case.id == case_id).first()
    if case is None:
        raise HTTPException(status_code=404, detail="Case not found")
    if not access_service.is_admin(user) and case.owner_id != user.id:
        # Don't reveal existence to non-owners.
        raise HTTPException(status_code=404, detail="Case not found")
    return case


@router.get("/{case_id}/sharing", response_class=HTMLResponse)
async def sharing_page(
    request: Request,
    case_id: str,
    db: Session = Depends(get_db),
    user: User = Depends(get_current_user),
):
    case = _require_owner_or_admin(db, user, case_id)
    shares = (
        db.query(CaseShare, User)
        .join(User, User.id == CaseShare.user_id)
        .filter(CaseShare.case_id == case_id)
        .all()
    )
    owner = db.get(User, case.owner_id) if case.owner_id else None
    return helpers.render_page(
        request,
        "pages/case_sharing.html",
        db=db,
        case=case,
        owner=owner,
        shares=[{"share": s, "user": u} for s, u in shares],
    )


@router.post("/{case_id}/shares")
async def add_share(
    case_id: str,
    email: str = Form(...),
    permission: str = Form("viewer"),
    db: Session = Depends(get_db),
    user: User = Depends(get_current_user),
):
    case = _require_owner_or_admin(db, user, case_id)
    target = auth_service.get_user_by_email(db, email)
    if target is None:
        return _toast("No user with that email.", "error", status=404)
    if target.id == case.owner_id:
        return _toast("That user already owns this case.", "error", status=400)

    level = CaseAccessLevel.EDITOR if permission == "editor" else CaseAccessLevel.VIEWER
    existing = (
        db.query(CaseShare)
        .filter(CaseShare.case_id == case_id, CaseShare.user_id == target.id)
        .first()
    )
    if existing:
        existing.permission = level
    else:
        db.add(
            CaseShare(
                case_id=case_id,
                user_id=target.id,
                permission=level,
                granted_by=user.id,
            )
        )
    db.commit()
    return _refresh("Access granted.")


@router.post("/{case_id}/shares/{user_id}/remove")
async def remove_share(
    case_id: str,
    user_id: int,
    db: Session = Depends(get_db),
    user: User = Depends(get_current_user),
):
    _require_owner_or_admin(db, user, case_id)
    share = (
        db.query(CaseShare)
        .filter(CaseShare.case_id == case_id, CaseShare.user_id == user_id)
        .first()
    )
    if share:
        db.delete(share)
        db.commit()
    return _refresh("Access removed.")
