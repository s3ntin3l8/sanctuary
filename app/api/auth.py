"""Local authentication routes: login, logout, signup, first-run admin setup.

These routes are on the public allowlist (see app.main._is_public_path). Login
and signup are rate-limited and return generic messages to avoid revealing
which emails are registered.
"""

from __future__ import annotations

import logging

from fastapi import APIRouter, Depends, Form, Request
from fastapi.responses import HTMLResponse, RedirectResponse, Response
from sqlalchemy.orm import Session

from app.config import templates
from app.core import security
from app.core.rate_limit import limiter
from app.dependencies import get_db
from app.models.enums import UserRole
from app.services import auth_service

logger = logging.getLogger(__name__)

router = APIRouter(tags=["auth"])

_MIN_PASSWORD_LEN = 8
_GENERIC_LOGIN_ERROR = "Invalid email or password."


def _safe_next(next_url: str | None) -> str:
    """Allow only same-site relative redirects (block open-redirects)."""
    if next_url and next_url.startswith("/") and not next_url.startswith("//"):
        return next_url
    return "/"


def _login_response(
    request: Request,
    *,
    next_url: str,
    signup_enabled: bool = False,
    error: str | None = None,
    status_code: int = 200,
) -> HTMLResponse:
    from app import config

    return templates.TemplateResponse(
        request,
        "login.html",
        {
            "sidebar_counts": {},
            "next": next_url,
            "error": error,
            "signup_enabled": signup_enabled,
            "oidc_enabled": config.oidc_enabled(),
            "oidc_provider_name": config.OIDC_PROVIDER_NAME,
        },
        status_code=status_code,
    )


def _signup_response(
    request: Request,
    *,
    first_run: bool,
    error: str | None = None,
    email: str = "",
    status_code: int = 200,
) -> HTMLResponse:
    return templates.TemplateResponse(
        request,
        "signup.html",
        {
            "sidebar_counts": {},
            "first_run": first_run,
            "error": error,
            "email": email,
        },
        status_code=status_code,
    )


@router.get("/login", response_class=HTMLResponse)
async def login_page(request: Request, next: str = "/", db: Session = Depends(get_db)):
    # Fresh install with no accounts → send to first-run admin creation.
    if auth_service.count_users(db) == 0:
        return RedirectResponse("/signup", status_code=303)
    return _login_response(
        request,
        next_url=_safe_next(next),
        signup_enabled=auth_service.signup_enabled(db),
    )


@router.post("/login", response_class=HTMLResponse)
@limiter.limit("10/minute")
async def login_submit(
    request: Request,
    email: str = Form(...),
    password: str = Form(...),
    next: str = Form("/"),
    db: Session = Depends(get_db),
):
    next_url = _safe_next(next)
    user = auth_service.get_user_by_email(db, email)
    valid, new_hash = security.verify_and_update(
        password, user.password_hash if user else None
    )
    if user is None or not user.is_active or not valid:
        return _login_response(
            request,
            next_url=next_url,
            signup_enabled=auth_service.signup_enabled(db),
            error=_GENERIC_LOGIN_ERROR,
            status_code=401,
        )

    if new_hash:
        user.password_hash = new_hash
    from app.core.timezone import naive_utc_now

    user.last_login_at = naive_utc_now()
    db.commit()

    # Session fixation: drop any prior session before writing the new identity.
    request.session.clear()
    request.session.update(auth_service.build_session(user))
    return RedirectResponse(next_url, status_code=303)


@router.post("/logout")
async def logout(request: Request):
    request.session.clear()
    return RedirectResponse("/login", status_code=303)


@router.get("/signup", response_class=HTMLResponse)
async def signup_page(request: Request, db: Session = Depends(get_db)):
    first_run = auth_service.count_users(db) == 0
    if not first_run and not auth_service.signup_enabled(db):
        return RedirectResponse("/login", status_code=303)
    return _signup_response(request, first_run=first_run)


@router.post("/signup", response_class=HTMLResponse)
@limiter.limit("10/minute")
async def signup_submit(
    request: Request,
    email: str = Form(...),
    password: str = Form(...),
    password_confirm: str = Form(""),
    display_name: str = Form(""),
    db: Session = Depends(get_db),
):
    first_run = auth_service.count_users(db) == 0
    if not first_run and not auth_service.signup_enabled(db):
        return Response(status_code=404)

    email = (email or "").strip().lower()
    if not email or "@" not in email:
        return _signup_response(
            request,
            first_run=first_run,
            error="Enter a valid email address.",
            email=email,
            status_code=422,
        )
    if len(password) < _MIN_PASSWORD_LEN:
        return _signup_response(
            request,
            first_run=first_run,
            error=f"Password must be at least {_MIN_PASSWORD_LEN} characters.",
            email=email,
            status_code=422,
        )
    if password_confirm and password != password_confirm:
        return _signup_response(
            request,
            first_run=first_run,
            error="Passwords do not match.",
            email=email,
            status_code=422,
        )

    # The very first account is always the admin.
    role = UserRole.ADMIN if first_run else UserRole.USER
    try:
        user = auth_service.create_user(
            db,
            email=email,
            password=password,
            role=role,
            display_name=display_name.strip() or None,
        )
    except auth_service.EmailAlreadyExists:
        # Generic message — don't confirm the email is registered.
        return _signup_response(
            request,
            first_run=first_run,
            error="Could not create the account. Try a different email.",
            email=email,
            status_code=409,
        )

    db.commit()
    request.session.clear()
    request.session.update(auth_service.build_session(user))
    return RedirectResponse("/", status_code=303)
