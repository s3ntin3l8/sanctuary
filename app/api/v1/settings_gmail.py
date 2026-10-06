"""Settings → Gmail: per-user mailbox connection, inbox filters and backfill."""

from __future__ import annotations

from fastapi import APIRouter, Depends, Request, Response
from sqlalchemy.orm import Session

from app.core.rate_limit import limiter
from app.dependencies import get_current_user, get_db
from app.models.database import User
from app.models.enums import AuditEventType
from app.schemas.settings import GmailBackfill, GmailFilters, GmailView
from app.services import audit_service, user_settings_service

router = APIRouter(prefix="/settings/gmail", tags=["settings"])

# The OAuth dance stays a server-side browser redirect (it is the URI
# registered with Google); the SPA only needs to know where to send the user.
OAUTH_START_URL = "/api/ingest/gmail/oauth/start"


def _view(db: Session, user: User) -> GmailView:
    cfg = user_settings_service.get_gmail_config(db, user.id)
    settings = user_settings_service._user_settings(db, user.id)
    data = (settings.settings_json or {}) if settings else {}
    return GmailView(
        connected=bool(cfg.get("gmail_credentials_json")),
        connected_at=cfg.get("gmail_connected_at"),
        last_sync_at=data.get("gmail_last_sync_at"),
        allowlist=list(cfg.get("gmail_allowlist") or []),
        label_filter=cfg.get("gmail_label_filter") or "",
        oauth_start_url=OAUTH_START_URL,
    )


@router.get("", response_model=GmailView)
def gmail(db: Session = Depends(get_db), user: User = Depends(get_current_user)):
    return _view(db, user)


@router.put("/filters", response_model=GmailView)
@limiter.limit("20/minute")
def save_filters(
    request: Request,
    body: GmailFilters,
    db: Session = Depends(get_db),
    user: User = Depends(get_current_user),
):
    user_settings_service.set_gmail_inbox_filters(
        db,
        user.id,
        allowlist=[e.strip() for e in body.allowlist if e.strip()],
        label_filter=body.label_filter.strip(),
    )
    audit_service.record(
        db, AuditEventType.SETTINGS_INGESTION_CHANGED, actor_user_id=user.id
    )
    db.commit()
    return _view(db, user)


@router.post("/backfill", status_code=202, response_class=Response)
@limiter.limit("2/minute")
def backfill(
    request: Request, body: GmailBackfill, user: User = Depends(get_current_user)
):
    """Queue a one-off import of the last ``days`` days of the user's mailbox."""
    from app.tasks.dispatch import dispatch_task
    from app.tasks.gmail_sync import run_gmail_backfill

    dispatch_task(run_gmail_backfill, user.id, days=body.days)
