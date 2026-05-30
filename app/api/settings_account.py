"""Per-user account settings: display name + password change."""

import json
import logging

from fastapi import APIRouter, Depends, Form, Request
from fastapi.responses import Response
from sqlalchemy.orm import Session

from app import helpers
from app.core import security
from app.dependencies import get_current_user, get_db
from app.models.database import User
from app.services import auth_service

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/api/settings/account", tags=["settings"])

_MIN_PASSWORD_LEN = 8


def _toast(message: str, kind: str = "success", status: int = 204) -> Response:
    resp = Response(status_code=status)
    resp.headers["HX-Trigger"] = json.dumps(helpers.toast_trigger(message, kind))
    return resp


@router.post("/profile")
async def update_profile(
    display_name: str = Form(""),
    db: Session = Depends(get_db),
    user: User = Depends(get_current_user),
):
    user.display_name = display_name.strip() or None
    db.commit()
    return _toast("Profile updated")


@router.post("/password")
async def change_password(
    request: Request,
    current_password: str = Form(...),
    new_password: str = Form(...),
    db: Session = Depends(get_db),
    user: User = Depends(get_current_user),
):
    if not security.verify_password(current_password, user.password_hash):
        return _toast("Current password is incorrect.", "error", status=422)
    if len(new_password) < _MIN_PASSWORD_LEN:
        return _toast(
            f"New password must be at least {_MIN_PASSWORD_LEN} characters.",
            "error",
            status=422,
        )

    # set_password bumps token_version (invalidating other sessions). Re-issue
    # THIS session with the new token_version so the current user stays logged in.
    auth_service.set_password(db, user, new_password)
    db.commit()
    request.session.clear()
    request.session.update(auth_service.build_session(user))
    return _toast("Password updated — other sessions signed out.")
