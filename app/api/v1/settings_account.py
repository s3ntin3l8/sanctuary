"""Settings → Account: profile, email and password of the signed-in user."""

from __future__ import annotations

from fastapi import APIRouter, Depends, Request, Response
from sqlalchemy.orm import Session

from app.api.v1.errors import ApiError
from app.core import security
from app.dependencies import get_current_user, get_db
from app.models.database import User
from app.schemas.settings import AccountView, EmailChange, PasswordChange, ProfileUpdate
from app.services import auth_service

router = APIRouter(prefix="/settings/account", tags=["settings"])

MIN_PASSWORD_LEN = 8


def _view(user: User) -> AccountView:
    return AccountView(
        email=user.email,
        display_name=user.display_name,
        role=user.role,
        has_password=bool(user.password_hash),
    )


@router.get("", response_model=AccountView)
def account(user: User = Depends(get_current_user)):
    return _view(user)


@router.put("/profile", response_model=AccountView)
def update_profile(
    body: ProfileUpdate,
    db: Session = Depends(get_db),
    user: User = Depends(get_current_user),
):
    user.display_name = body.display_name.strip() or None
    db.commit()
    return _view(user)


@router.put("/email", response_model=AccountView)
def change_email(
    body: EmailChange,
    db: Session = Depends(get_db),
    user: User = Depends(get_current_user),
):
    # Gate the credential change on the current password, except for accounts
    # that have none yet (e.g. an auth-disabled bootstrap admin).
    if user.password_hash and not security.verify_password(
        body.current_password, user.password_hash
    ):
        raise ApiError(422, "wrong_password", "Current password is incorrect.")
    try:
        auth_service.change_email(db, user, body.new_email)
    except auth_service.InvalidEmail as exc:
        raise ApiError(422, "invalid_email", "Enter a valid email address.") from exc
    except auth_service.EmailAlreadyExists as exc:
        raise ApiError(
            422, "email_unavailable", "That email is already in use."
        ) from exc
    db.commit()
    return _view(user)


@router.put("/password", status_code=204, response_class=Response)
def change_password(
    body: PasswordChange,
    request: Request,
    db: Session = Depends(get_db),
    user: User = Depends(get_current_user),
):
    if not security.verify_password(body.current_password, user.password_hash):
        raise ApiError(422, "wrong_password", "Current password is incorrect.")
    if len(body.new_password) < MIN_PASSWORD_LEN:
        raise ApiError(
            422,
            "password_too_short",
            f"New password must be at least {MIN_PASSWORD_LEN} characters.",
        )
    # set_password bumps token_version (signing out other sessions); re-issue
    # this session so the current user stays logged in.
    auth_service.set_password(db, user, body.new_password)
    db.commit()
    request.session.clear()
    request.session.update(auth_service.build_session(user))
