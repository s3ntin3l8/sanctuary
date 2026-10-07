import logging
import secrets

from fastapi import APIRouter, Depends, HTTPException, Request
from fastapi.responses import RedirectResponse
from sqlalchemy.orm import Session

from app.core.rate_limit import limiter
from app.core.timezone import now_utc
from app.dependencies import get_current_user, get_db
from app.models.database import User
from app.services import user_settings_service
from app.services.ingestion.gmail import (
    GmailScopeError,
    assert_readonly_scopes,
    get_oauth_flow,
)

logger = logging.getLogger(__name__)
router = APIRouter(prefix="/api/ingest", tags=["ingestion"])

OAUTH_STATE_COOKIE = "oauth_state"


@router.get("/gmail/oauth/start")
@limiter.limit("20/minute")
async def gmail_oauth_start(request: Request):
    state = secrets.token_urlsafe(32)
    request.session[OAUTH_STATE_COOKIE] = state
    flow = get_oauth_flow()
    authorization_url, _ = flow.authorization_url(
        access_type="offline",
        # No include_granted_scopes: it would fold scopes granted to this OAuth
        # client earlier into the new token. We want exactly gmail.readonly.
        state=state,
    )
    return RedirectResponse(url=authorization_url)


@router.get("/gmail/oauth/callback")
@limiter.limit("20/minute")
async def gmail_oauth_callback(
    request: Request,
    code: str,
    state: str | None = None,
    db: Session = Depends(get_db),
    user: User = Depends(get_current_user),
):
    saved_state = request.session.pop(OAUTH_STATE_COOKIE, None)
    if state != saved_state:
        logger.warning("OAuth state mismatch: expected=%s got=%s", saved_state, state)
        raise HTTPException(status_code=400, detail="OAuth state mismatch")

    flow = get_oauth_flow()
    try:
        flow.fetch_token(code=code)
    except Warning as exc:
        # oauthlib raises a bare Warning when Google returns different scopes
        # than were requested — i.e. more than read-only access.
        logger.warning("Gmail OAuth scope mismatch: %s", exc)
        raise HTTPException(
            status_code=400,
            detail="Google granted different permissions than requested; "
            "Sanctuary only accepts read-only Gmail access.",
        ) from exc
    creds = flow.credentials
    try:
        assert_readonly_scopes(creds.granted_scopes)
    except GmailScopeError as exc:
        logger.warning("Gmail OAuth rejected: %s", exc)
        raise HTTPException(status_code=400, detail=str(exc)) from exc

    user_settings_service.set_gmail_credentials(
        db,
        user.id,
        credentials_json=creds.to_json(),
        connected_at=now_utc().isoformat(),
    )
    db.commit()

    return RedirectResponse(url="/settings/gmail")
