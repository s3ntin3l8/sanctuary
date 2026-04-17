import logging
from datetime import datetime

from fastapi import APIRouter, Depends, Form, Request
from fastapi.responses import HTMLResponse, RedirectResponse
from sqlalchemy.orm import Session

from app.config import templates
from app.dependencies import get_db
from app.models.database import UserSettings
from app.services.ingestion.gmail import get_oauth_flow
from app.tasks.gmail_sync import run_gmail_backfill

logger = logging.getLogger(__name__)
router = APIRouter(prefix="/api/ingest", tags=["ingestion"])

@router.get("/settings", response_class=HTMLResponse)
async def get_ingest_settings(request: Request, db: Session = Depends(get_db)):
    settings = db.query(UserSettings).filter(UserSettings.user_id == "single_user").first()
    if not settings:
        settings = UserSettings(user_id="single_user")
        db.add(settings)
        db.commit()
        db.refresh(settings)

    sidebar_counts = {
        "triage_count": 0, # Should fetch real counts here
        "pending_count": 0,
        "case_count": 0,
        "cost_count": 0,
    }

    return templates.TemplateResponse(
        request,
        "pages/gmail_settings.html",
        {
            "settings": settings.settings_json,
            "sidebar_counts": sidebar_counts,
        }
    )

@router.post("/settings/update")
async def update_ingest_settings(
    allowlist: str = Form(""),
    label_filter: str = Form(""),
    db: Session = Depends(get_db)
):
    settings = db.query(UserSettings).filter(UserSettings.user_id == "single_user").first()
    if not settings:
        settings = UserSettings(user_id="single_user")
        db.add(settings)

    s_json = dict(settings.settings_json)
    s_json["gmail_allowlist"] = [e.strip() for e in allowlist.split(",") if e.strip()]
    s_json["gmail_label_filter"] = label_filter.strip()

    settings.settings_json = s_json
    db.commit()

    return RedirectResponse(url="/api/ingest/settings", status_code=303)

@router.get("/gmail/oauth/start")
async def gmail_oauth_start():
    flow = get_oauth_flow()
    authorization_url, state = flow.authorization_url(access_type="offline", include_granted_scopes="true")
    # In a real app, save 'state' in session/secure cookie to prevent CSRF
    return RedirectResponse(url=authorization_url)

@router.get("/gmail/oauth/callback")
async def gmail_oauth_callback(code: str, db: Session = Depends(get_db)):
    flow = get_oauth_flow()
    flow.fetch_token(code=code)
    creds = flow.credentials

    settings = db.query(UserSettings).filter(UserSettings.user_id == "single_user").first()
    if not settings:
        settings = UserSettings(user_id="single_user")
        db.add(settings)

    s_json = dict(settings.settings_json)
    s_json["gmail_credentials_json"] = creds.to_json()
    s_json["gmail_connected_at"] = datetime.now().isoformat()

    settings.settings_json = s_json
    db.commit()

    return RedirectResponse(url="/api/ingest/settings")

@router.post("/gmail/backfill")
async def gmail_backfill(days: int = Form(90)):
    # Trigger Celery task
    run_gmail_backfill.delay("single_user", days=days)
    return {"status": "Backfill task enqueued"}
