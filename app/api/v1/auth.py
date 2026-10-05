"""Local authentication for the SPA: sign-in config, login, signup.

Public (see ``app.main._is_public_path``). Login and signup are rate-limited
and return generic messages so they never reveal which emails are registered.
"""

from __future__ import annotations

from fastapi import APIRouter, Depends, Request
from sqlalchemy.orm import Session

from app import config
from app.api.v1.errors import ApiError
from app.core import security
from app.core.rate_limit import limiter
from app.core.timezone import now_utc
from app.dependencies import get_db
from app.models.enums import UserRole
from app.schemas.auth import AuthConfig, LoginRequest, SessionStarted, SignupRequest
from app.services import auth_service

router = APIRouter(prefix="/auth", tags=["auth"])

MIN_PASSWORD_LEN = 8


def safe_next(next_url: str | None) -> str:
    """Allow only same-site relative redirects (block open-redirects).

    Rejects any leading "//" (protocol-relative) or "/\\" — browsers
    normalize a leading backslash to a forward slash, so "/\\evil.com"
    would otherwise be treated as relative here but resolve as
    protocol-relative in the browser.
    """
    if (
        next_url
        and next_url.startswith("/")
        and not next_url.startswith("//")
        and not next_url.startswith("/\\")
    ):
        return next_url
    return "/"


@router.get("/config", response_model=AuthConfig)
def auth_config(db: Session = Depends(get_db)) -> AuthConfig:
    return AuthConfig(
        first_run=auth_service.count_users(db) == 0,
        signup_enabled=auth_service.signup_enabled(db),
        oidc_enabled=config.oidc_enabled(),
        oidc_provider_name=config.OIDC_PROVIDER_NAME,
    )


@router.post("/login", response_model=SessionStarted)
@limiter.limit("10/minute")
def login(
    request: Request, body: LoginRequest, db: Session = Depends(get_db)
) -> SessionStarted:
    user = auth_service.get_user_by_email(db, body.email)
    valid, new_hash = security.verify_and_update(
        body.password, user.password_hash if user else None
    )
    if user is None or not user.is_active or not valid:
        raise ApiError(401, "invalid_credentials", "Invalid email or password.")

    if new_hash:
        user.password_hash = new_hash
    user.last_login_at = now_utc()
    db.commit()

    # Session fixation: drop any prior session before writing the new identity.
    request.session.clear()
    request.session.update(auth_service.build_session(user))
    return SessionStarted(next=safe_next(body.next))


@router.post("/signup", response_model=SessionStarted)
@limiter.limit("10/minute")
def signup(
    request: Request, body: SignupRequest, db: Session = Depends(get_db)
) -> SessionStarted:
    first_run = auth_service.count_users(db) == 0
    if not first_run and not auth_service.signup_enabled(db):
        raise ApiError(404, "signup_disabled", "Sign-up is not available.")

    email = body.email.strip().lower()
    if not email or "@" not in email:
        raise ApiError(422, "invalid_email", "Enter a valid email address.")
    if len(body.password) < MIN_PASSWORD_LEN:
        raise ApiError(
            422,
            "password_too_short",
            f"Password must be at least {MIN_PASSWORD_LEN} characters.",
        )
    if body.password != body.password_confirm:
        raise ApiError(422, "password_mismatch", "Passwords do not match.")

    # The very first account is always the admin.
    role = UserRole.ADMIN if first_run else UserRole.USER
    try:
        user = auth_service.create_user(
            db,
            email=email,
            password=body.password,
            role=role,
            display_name=body.display_name.strip() or None,
        )
    except auth_service.EmailAlreadyExists as exc:
        # Generic message — don't confirm the email is registered.
        raise ApiError(
            409,
            "email_unavailable",
            "Could not create the account. Try a different email.",
        ) from exc

    if first_run:
        # The first account is the primary admin — pin it by id so worker and
        # dev-mode code paths resolve it without re-deriving from any string.
        auth_service.set_bootstrap_admin_id(db, user.id)
    db.commit()
    request.session.clear()
    request.session.update(auth_service.build_session(user))
    return SessionStarted(next="/")
