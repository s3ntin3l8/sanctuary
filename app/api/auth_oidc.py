"""OIDC / authentik login (Phase 2).

Authorization-code flow via Authlib's Starlette client, which stores the
``state`` (CSRF) and ``nonce`` (replay) in ``request.session`` — backed by the
app's signed-cookie session middleware. Active only when OIDC is configured
(issuer + client id + secret); otherwise these routes 404 and the login page
hides the button.
"""

from __future__ import annotations

import logging

from authlib.integrations.starlette_client import OAuth, OAuthError
from fastapi import APIRouter, Depends, Request
from fastapi.responses import RedirectResponse
from sqlalchemy.orm import Session

from app import config
from app.config import templates
from app.dependencies import get_db
from app.services import auth_service

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/auth/oidc", tags=["auth"])

_oauth: OAuth | None = None


def _get_client():
    """Lazily build (and cache) the Authlib OIDC client from current config."""
    global _oauth
    if _oauth is None:
        oauth = OAuth()
        oauth.register(
            name="oidc",
            client_id=config.OIDC_CLIENT_ID,
            client_secret=config.OIDC_CLIENT_SECRET,
            server_metadata_url=(
                f"{config.OIDC_ISSUER}/.well-known/openid-configuration"
            ),
            client_kwargs={"scope": config.OIDC_SCOPES},
        )
        _oauth = oauth
    return _oauth.oidc


def reset_client() -> None:
    """Clear the cached client (tests / config changes)."""
    global _oauth
    _oauth = None


@router.get("/login")
async def oidc_login(request: Request):
    if not config.oidc_enabled():
        from fastapi import HTTPException

        raise HTTPException(status_code=404)
    client = _get_client()
    return await client.authorize_redirect(request, config.OIDC_REDIRECT_URI)


@router.get("/callback")
async def oidc_callback(request: Request, db: Session = Depends(get_db)):
    if not config.oidc_enabled():
        from fastapi import HTTPException

        raise HTTPException(status_code=404)

    client = _get_client()
    try:
        token = await client.authorize_access_token(request)
    except OAuthError as exc:
        logger.warning("OIDC callback failed: %s", exc)
        return _login_error(request, "Single sign-on failed. Please try again.")

    userinfo = token.get("userinfo") or {}
    subject = userinfo.get("sub")
    if not subject:
        return _login_error(request, "Single sign-on returned no identity.")
    email = userinfo.get("email")
    display_name = userinfo.get("name") or userinfo.get("preferred_username")

    user = auth_service.link_or_create_oidc_user(
        db,
        issuer=config.OIDC_ISSUER,
        subject=subject,
        email=email,
        display_name=display_name,
        signup_allowed=auth_service.signup_enabled(db),
    )
    if user is None:
        return _login_error(
            request,
            "No account is linked to this identity. Ask an administrator for access.",
        )

    from app.core.timezone import now_utc

    user.last_login_at = now_utc()
    db.commit()

    request.session.clear()
    request.session.update(auth_service.build_session(user))
    return RedirectResponse("/", status_code=303)


def _login_error(request: Request, message: str):
    return templates.TemplateResponse(
        request,
        "login.html",
        {"sidebar_counts": {}, "next": "/", "error": message, "signup_enabled": False},
        status_code=401,
    )
