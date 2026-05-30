"""Admin-only user management: list, create, toggle active, set role, reset
password, delete, and the runtime signup toggle. Guarded by get_current_admin."""

import json
import logging

from fastapi import APIRouter, Depends, Form, Request
from fastapi.responses import HTMLResponse, Response
from sqlalchemy.orm import Session

from app import helpers
from app.dependencies import get_current_admin, get_db
from app.models.database import Case, User
from app.models.enums import UserRole
from app.services import auth_service

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/admin", tags=["admin"])

_MIN_PASSWORD_LEN = 8


def _toast(message: str, kind: str = "success", status: int = 204) -> Response:
    resp = Response(status_code=status)
    resp.headers["HX-Trigger"] = json.dumps(helpers.toast_trigger(message, kind))
    return resp


def _refresh(message: str, kind: str = "success") -> Response:
    resp = Response(status_code=204)
    resp.headers["HX-Trigger"] = json.dumps(
        {**helpers.toast_trigger(message, kind), "refreshUsers": True}
    )
    resp.headers["HX-Refresh"] = "true"
    return resp


@router.get("/users", response_class=HTMLResponse)
async def users_list(
    request: Request,
    db: Session = Depends(get_db),
    admin: User = Depends(get_current_admin),
):
    users = db.query(User).order_by(User.created_at.asc()).all()
    return helpers.render_page(
        request,
        "pages/admin/users.html",
        db=db,
        users=users,
        signup_enabled=auth_service.signup_enabled(db),
    )


@router.post("/users")
async def create_user(
    email: str = Form(...),
    password: str = Form(...),
    role: str = Form("user"),
    db: Session = Depends(get_db),
    admin: User = Depends(get_current_admin),
):
    if len(password) < _MIN_PASSWORD_LEN:
        return _toast(
            f"Password must be at least {_MIN_PASSWORD_LEN} characters.",
            "error",
            status=422,
        )
    role_enum = UserRole.ADMIN if role == "admin" else UserRole.USER
    try:
        auth_service.create_user(db, email=email, password=password, role=role_enum)
    except auth_service.EmailAlreadyExists:
        return _toast("That email is already registered.", "error", status=409)
    db.commit()
    return _refresh("User created.")


@router.post("/users/{user_id}/toggle-active")
async def toggle_active(
    user_id: int,
    db: Session = Depends(get_db),
    admin: User = Depends(get_current_admin),
):
    user = db.get(User, user_id)
    if user is None:
        return _toast("User not found.", "error", status=404)
    if user.id == admin.id:
        return _toast("You cannot deactivate your own account.", "error", status=400)
    user.is_active = not user.is_active
    auth_service.bump_token_version(user)  # kill their sessions
    db.commit()
    return _refresh("Active" if user.is_active else "Deactivated")


@router.post("/users/{user_id}/role")
async def set_role(
    user_id: int,
    role: str = Form(...),
    db: Session = Depends(get_db),
    admin: User = Depends(get_current_admin),
):
    user = db.get(User, user_id)
    if user is None:
        return _toast("User not found.", "error", status=404)
    if user.id == admin.id:
        return _toast("You cannot change your own role.", "error", status=400)
    user.role = UserRole.ADMIN if role == "admin" else UserRole.USER
    db.commit()
    return _refresh("Role updated.")


@router.post("/users/{user_id}/reset-password")
async def reset_password(
    user_id: int,
    new_password: str = Form(...),
    db: Session = Depends(get_db),
    admin: User = Depends(get_current_admin),
):
    user = db.get(User, user_id)
    if user is None:
        return _toast("User not found.", "error", status=404)
    if len(new_password) < _MIN_PASSWORD_LEN:
        return _toast(
            f"Password must be at least {_MIN_PASSWORD_LEN} characters.",
            "error",
            status=422,
        )
    auth_service.set_password(db, user, new_password)  # bumps token_version
    db.commit()
    return _toast("Password reset — that user's sessions were signed out.")


@router.post("/users/{user_id}/delete")
async def delete_user(
    user_id: int,
    db: Session = Depends(get_db),
    admin: User = Depends(get_current_admin),
):
    user = db.get(User, user_id)
    if user is None:
        return _toast("User not found.", "error", status=404)
    if user.id == admin.id:
        return _toast("You cannot delete your own account.", "error", status=400)
    owned = db.query(Case).filter(Case.owner_id == user.id).count()
    if owned:
        return _toast(
            f"User owns {owned} case(s). Reassign them before deleting.",
            "error",
            status=409,
        )
    db.delete(user)
    db.commit()
    return _refresh("User deleted.")


@router.post("/users/{user_id}/reassign-cases")
async def reassign_cases(
    user_id: int,
    new_owner_id: int = Form(...),
    db: Session = Depends(get_db),
    admin: User = Depends(get_current_admin),
):
    """Reassign all cases owned by `user_id` to `new_owner_id` (unblocks delete)."""
    if db.get(User, new_owner_id) is None:
        return _toast("Target owner not found.", "error", status=404)
    n = (
        db.query(Case)
        .filter(Case.owner_id == user_id)
        .update({Case.owner_id: new_owner_id}, synchronize_session=False)
    )
    db.commit()
    return _refresh(f"Reassigned {n} case(s).")


@router.post("/signup-toggle")
async def toggle_signup(
    db: Session = Depends(get_db),
    admin: User = Depends(get_current_admin),
):
    new_value = not auth_service.signup_enabled(db)
    auth_service.set_signup_enabled(db, new_value)
    db.commit()
    return _refresh("Signup enabled." if new_value else "Signup disabled.")
