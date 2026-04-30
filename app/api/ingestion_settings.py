import logging
import secrets
from datetime import datetime

from fastapi import APIRouter, Depends, Form, HTTPException, Request
from fastapi.responses import RedirectResponse
from sqlalchemy.orm import Session

from app.dependencies import get_db
from app.models.database import UserSettings
from app.services.ingestion.gmail import get_oauth_flow
from app.tasks.gmail_sync import run_gmail_backfill

logger = logging.getLogger(__name__)
router = APIRouter(prefix="/api/ingest", tags=["ingestion"])

OAUTH_STATE_COOKIE = "oauth_state"


@router.post("/settings/update")
async def update_ingest_settings(
    allowlist: str = Form(""),
    label_filter: str = Form(""),
    db: Session = Depends(get_db),
):
    settings = (
        db.query(UserSettings).filter(UserSettings.user_id == "single_user").first()
    )
    if not settings:
        settings = UserSettings(user_id="single_user")
        db.add(settings)

    s_json = dict(settings.settings_json)
    s_json["gmail_allowlist"] = [e.strip() for e in allowlist.split(",") if e.strip()]
    s_json["gmail_label_filter"] = label_filter.strip()

    settings.settings_json = s_json
    db.commit()

    return RedirectResponse(url="/settings/gmail", status_code=303)


@router.get("/gmail/oauth_start")
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
async def gmail_oauth_callback(
    request: Request,
    code: str,
    state: str | None = None,
    db: Session = Depends(get_db),
):
    saved_state = request.session.pop(OAUTH_STATE_COOKIE, None)
    if state != saved_state:
        logger.warning("OAuth state mismatch: expected=%s got=%s", saved_state, state)
        raise HTTPException(status_code=400, detail="OAuth state mismatch")

    flow = get_oauth_flow()
    flow.fetch_token(code=code)
    creds = flow.credentials

    settings = (
        db.query(UserSettings).filter(UserSettings.user_id == "single_user").first()
    )
    if not settings:
        settings = UserSettings(user_id="single_user")
        db.add(settings)

    s_json = dict(settings.settings_json)
    s_json["gmail_credentials_json"] = creds.to_json()
    s_json["gmail_connected_at"] = datetime.now().isoformat()

    settings.settings_json = s_json
    db.commit()

    return RedirectResponse(url="/settings/gmail")


@router.post("/gmail/backfill")
async def gmail_backfill(days: int = Form(90)):
    from app.tasks.dispatch import dispatch_task

    dispatch_task(run_gmail_backfill, "single_user", days=days)
    return {"status": "Backfill task enqueued"}
