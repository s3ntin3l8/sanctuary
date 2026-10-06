"""Admin → Users: account management (admins only)."""

from __future__ import annotations

from fastapi import APIRouter, Depends
from sqlalchemy import func
from sqlalchemy.orm import Session

from app.api.v1.errors import ApiError
from app.dependencies import get_current_admin, get_db
from app.models.database import Case, User
from app.schemas.admin import (
    AdminActiveUpdate,
    AdminPasswordReset,
    AdminReassignCases,
    AdminRoleUpdate,
    AdminUser,
    AdminUserCreate,
    AdminUsersView,
    SignupToggle,
)
from app.services import auth_service

router = APIRouter(prefix="/admin", tags=["admin"])


def _view(db: Session) -> AdminUsersView:
    owned: dict[int, int] = {
        owner_id: count
        for owner_id, count in db.query(Case.owner_id, func.count(Case.id))
        .filter(Case.owner_id.isnot(None))
        .group_by(Case.owner_id)
        .all()
        if owner_id is not None
    }
    users = db.query(User).order_by(User.created_at.asc()).all()
    return AdminUsersView(
        users=[
            AdminUser(
                id=u.id,
                email=u.email,
                display_name=u.display_name,
                role=u.role,
                is_active=u.is_active,
                created_at=u.created_at,
                last_login_at=u.last_login_at,
                owned_case_count=owned.get(u.id, 0),
            )
            for u in users
        ],
        signup_enabled=auth_service.signup_enabled(db),
    )


def _target(db: Session, user_id: int) -> User:
    user = db.get(User, user_id)
    if user is None:
        raise ApiError(404, "not_found", "User not found.")
    return user


@router.get("/users", response_model=AdminUsersView)
def users(db: Session = Depends(get_db), admin: User = Depends(get_current_admin)):
    return _view(db)


@router.post("/users", response_model=AdminUsersView, status_code=201)
def create_user(
    body: AdminUserCreate,
    db: Session = Depends(get_db),
    admin: User = Depends(get_current_admin),
):
    email = body.email.strip().lower()
    if not email or "@" not in email:
        raise ApiError(422, "invalid_email", "Enter a valid email address.")
    try:
        auth_service.create_user(
            db, email=email, password=body.password, role=body.role
        )
    except auth_service.EmailAlreadyExists as exc:
        raise ApiError(
            409, "email_unavailable", "That email is already in use."
        ) from exc
    db.commit()
    return _view(db)


@router.put("/users/{user_id}/active", response_model=AdminUsersView)
def set_active(
    user_id: int,
    body: AdminActiveUpdate,
    db: Session = Depends(get_db),
    admin: User = Depends(get_current_admin),
):
    user = _target(db, user_id)
    if user.id == admin.id:
        raise ApiError(400, "self_change", "You cannot deactivate your own account.")
    if user.is_active != body.is_active:
        user.is_active = body.is_active
        auth_service.bump_token_version(user)
        db.commit()
    return _view(db)


@router.put("/users/{user_id}/role", response_model=AdminUsersView)
def set_user_role(
    user_id: int,
    body: AdminRoleUpdate,
    db: Session = Depends(get_db),
    admin: User = Depends(get_current_admin),
):
    user = _target(db, user_id)
    if user.id == admin.id:
        raise ApiError(400, "self_change", "You cannot change your own role.")
    user.role = body.role
    db.commit()
    return _view(db)


@router.put("/users/{user_id}/password", status_code=204)
def reset_password(
    user_id: int,
    body: AdminPasswordReset,
    db: Session = Depends(get_db),
    admin: User = Depends(get_current_admin),
):
    user = _target(db, user_id)
    if user.id == admin.id:
        raise ApiError(
            400, "self_change", "Change your own password under Settings → Account."
        )
    auth_service.set_password(db, user, body.new_password)
    db.commit()


@router.delete("/users/{user_id}", response_model=AdminUsersView)
def delete_user(
    user_id: int,
    db: Session = Depends(get_db),
    admin: User = Depends(get_current_admin),
):
    user = _target(db, user_id)
    if user.id == admin.id:
        raise ApiError(400, "self_change", "You cannot delete your own account.")
    owned = db.query(Case).filter(Case.owner_id == user.id).count()
    if owned:
        raise ApiError(
            409,
            "owns_cases",
            f"User owns {owned} case(s). Reassign them before deleting.",
        )
    db.delete(user)
    db.commit()
    return _view(db)


@router.post("/users/{user_id}/reassign-cases", response_model=AdminUsersView)
def reassign_cases(
    user_id: int,
    body: AdminReassignCases,
    db: Session = Depends(get_db),
    admin: User = Depends(get_current_admin),
):
    """Move every case owned by ``user_id`` to ``new_owner_id`` (unblocks delete)."""
    _target(db, user_id)
    if body.new_owner_id == user_id:
        raise ApiError(400, "same_owner", "Pick a different owner.")
    _target(db, body.new_owner_id)
    db.query(Case).filter(Case.owner_id == user_id).update(
        {Case.owner_id: body.new_owner_id}, synchronize_session=False
    )
    db.commit()
    return _view(db)


@router.put("/signup", response_model=AdminUsersView)
def set_signup(
    body: SignupToggle,
    db: Session = Depends(get_db),
    admin: User = Depends(get_current_admin),
):
    auth_service.set_signup_enabled(db, body.enabled)
    db.commit()
    return _view(db)
