import logging
import secrets
from datetime import datetime

from fastapi import APIRouter, Depends, Form, HTTPException, Request
from fastapi.responses import RedirectResponse
from sqlalchemy.orm import Session

from app.core.rate_limit import limiter
from app.dependencies import get_current_user, get_db
from app.models.database import User
from app.models.enums import AuditEventType
from app.services import audit_service, user_settings_service
from app.services.ingestion.gmail import get_oauth_flow
from app.tasks.gmail_sync import run_gmail_backfill

logger = logging.getLogger(__name__)
router = APIRouter(prefix="/api/ingest", tags=["ingestion"])

OAUTH_STATE_COOKIE = "oauth_state"


@router.post("/settings/update")
@limiter.limit("20/minute")
async def update_ingest_settings(
    request: Request,
    allowlist: str = Form(""),
    label_filter: str = Form(""),
    db: Session = Depends(get_db),
    user: User = Depends(get_current_user),
):
    user_settings_service.set_gmail_inbox_filters(
        db,
        user.id,
        allowlist=[e.strip() for e in allowlist.split(",") if e.strip()],
        label_filter=label_filter.strip(),
    )
    audit_service.record(
        db, AuditEventType.SETTINGS_INGESTION_CHANGED, actor_user_id=user.id
    )
    db.commit()

    return RedirectResponse(url="/settings/gmail", status_code=303)


@router.get("/gmail/oauth/start")
@limiter.limit("20/minute")
async def gmail_oauth_start(request: Request):
    state = secrets.token_urlsafe(32)
    request.session[OAUTH_STATE_COOKIE] = state
    flow = get_oauth_flow()
    authorization_url, _ = flow.authorization_url(
        access_type="offline",
        include_granted_scopes="true",
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
    flow.fetch_token(code=code)
    creds = flow.credentials

    user_settings_service.set_gmail_credentials(
        db,
        user.id,
        credentials_json=creds.to_json(),
        connected_at=datetime.now().isoformat(),
    )
    db.commit()

    return RedirectResponse(url="/settings/gmail")


@router.post("/gmail/backfill")
@limiter.limit("2/minute")
async def gmail_backfill(
    request: Request,
    days: int = Form(90),
    user: User = Depends(get_current_user),
):
    from app.tasks.dispatch import dispatch_task

    dispatch_task(run_gmail_backfill, user.id, days=days)
    return {"status": "Backfill task enqueued"}
