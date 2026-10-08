"""Settings → Gmail: per-user mailbox connection, inbox filters and sync controls."""

from __future__ import annotations

import logging
from datetime import UTC, datetime, time

import httplib2
from fastapi import APIRouter, Depends, Request, Response
from google.auth.exceptions import TransportError
from googleapiclient.errors import HttpError
from sqlalchemy.orm import Session

from app.api.v1.errors import ApiError
from app.core.rate_limit import limiter
from app.core.secrets import SecretsError
from app.dependencies import get_current_user, get_db
from app.models.database import User
from app.models.enums import AuditEventType
from app.schemas.settings import (
    GmailFilterPreview,
    GmailFilters,
    GmailResetSync,
    GmailSyncMode,
    GmailView,
)
from app.services import (
    audit_service,
    gmail_index_service,
    gmail_runs,
    user_settings_service,
)
from app.services.ai_config import (
    get_chat_config,
    get_embed_config,
    get_ocr_config,
    is_external_endpoint,
)
from app.services.ingestion.gmail import (
    GmailReconnectRequired,
    build_query,
    connect_gmail,
    estimate_matches,
    revoke_token,
)

# What a Gmail call raises when Google or the network misbehaves. Anything else is
# a programming error and should surface as a 500 in the logs, not a "502".
_GMAIL_TRANSPORT_ERRORS = (HttpError, httplib2.HttpLib2Error, TransportError, OSError)

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/settings/gmail", tags=["settings"])

# The OAuth dance stays a server-side browser redirect (it is the URI
# registered with Google); the SPA only needs to know where to send the user.
OAUTH_START_URL = "/api/ingest/gmail/oauth/start"


def _ai_external(db: Session) -> bool:
    try:
        return any(
            is_external_endpoint(cfg.base_url)
            for cfg in (get_chat_config(db), get_embed_config(db), get_ocr_config(db))
        )
    except SecretsError:
        return False  # AI keys unreadable: AI calls fail anyway, nothing leaves


def _view(db: Session, user: User) -> GmailView:
    cfg = user_settings_service.get_gmail_config(db, user.id)
    return GmailView(
        connected=bool(cfg.get("gmail_credentials_json")),
        connected_at=cfg.get("gmail_connected_at"),
        last_sync_at=cfg.get("gmail_last_sync_at"),
        allowlist=list(cfg.get("gmail_allowlist") or []),
        label_filter=cfg.get("gmail_label_filter") or "",
        oauth_start_url=OAUTH_START_URL,
        sync_mode=cfg.get("gmail_sync_mode") or "off",
        last_check_at=cfg.get("gmail_last_check_at"),
        last_sync_result=cfg.get("gmail_last_sync_result"),
        last_sync_error=cfg.get("gmail_last_sync_error"),
        reconnect_required=bool(cfg.get("gmail_reconnect_required")),
        failed_count=len(cfg.get("gmail_failed_message_ids") or []),
        ai_external=_ai_external(db),
    )


def _audit(db: Session, user: User, action: str) -> None:
    audit_service.record(
        db,
        AuditEventType.SETTINGS_INGESTION_CHANGED,
        actor_user_id=user.id,
        payload={"gmail": action},
    )


def _require_connected(db: Session, user: User) -> None:
    if not user_settings_service.get_gmail_config(db, user.id).get(
        "gmail_credentials_json"
    ):
        raise ApiError(409, "gmail_not_connected", "Connect Gmail first.")


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
        allowlist=body.allowlist,
        label_filter=body.label_filter,
    )
    _audit(db, user, "filters")
    db.commit()
    return _view(db, user)


@router.post("/filters/preview", response_model=GmailFilterPreview)
@limiter.limit("10/minute")
def preview_filters(
    request: Request,
    body: GmailFilters,
    db: Session = Depends(get_db),
    user: User = Depends(get_current_user),
):
    """How many messages these (not yet saved) filters would match — one read-only
    list call, so an over-broad filter is obvious before it is saved."""
    _require_connected(db, user)
    sj = user_settings_service.get_gmail_config(db, user.id)
    try:
        service = connect_gmail(db, user.id, sj)
        estimate = estimate_matches(
            service, build_query(body.allowlist, body.label_filter)
        )
    except GmailReconnectRequired as exc:
        raise ApiError(409, "gmail_reconnect_required", str(exc)) from exc
    except _GMAIL_TRANSPORT_ERRORS as exc:  # Google/network failure, not a bug
        logger.warning("Gmail filter preview failed for user %d: %s", user.id, exc)
        raise ApiError(
            502, "gmail_unreachable", "Couldn't reach Gmail to count matches."
        ) from exc
    return GmailFilterPreview(estimate=estimate)


@router.put("/sync-mode", response_model=GmailView)
@limiter.limit("20/minute")
def set_sync_mode(
    request: Request,
    body: GmailSyncMode,
    db: Session = Depends(get_db),
    user: User = Depends(get_current_user),
):
    """Off, notify (index new mail and ask) or auto (import new mail as it arrives)."""
    _require_connected(db, user)
    user_settings_service.set_gmail_sync_mode(db, user.id, body.mode)
    _audit(db, user, f"sync_mode_{body.mode}")
    db.commit()
    return _view(db, user)


@router.post("/sync", status_code=202, response_class=Response)
@limiter.limit("6/minute")
def sync_now(
    request: Request,
    db: Session = Depends(get_db),
    user: User = Depends(get_current_user),
):
    """Run one incremental sync now, regardless of the auto-sync switch."""
    from app.tasks.dispatch import dispatch_task
    from app.tasks.gmail_sync import sync_gmail_for_user

    _require_connected(db, user)
    dispatch_task(sync_gmail_for_user, user.id)


@router.post("/reset-sync", response_model=GmailView)
@limiter.limit("6/minute")
def reset_sync(
    request: Request,
    body: GmailResetSync,
    db: Session = Depends(get_db),
    user: User = Depends(get_current_user),
):
    """Forget tracked failures and move the sync watermark (default: now)."""
    _require_connected(db, user)
    since = (
        datetime.combine(body.since, time.min, tzinfo=UTC)
        if body.since
        else datetime.now(UTC)
    )
    user_settings_service.reset_gmail_sync(db, user.id, since=since.isoformat())
    _audit(db, user, "reset_sync")
    db.commit()
    return _view(db, user)


@router.delete("", response_model=GmailView)
@limiter.limit("6/minute")
def disconnect(
    request: Request,
    db: Session = Depends(get_db),
    user: User = Depends(get_current_user),
):
    """Forget the Gmail grant locally and revoke it at Google (best effort).

    Revoking only cancels Sanctuary's own access; nothing in the mailbox is
    touched. Already-ingested mail stays.
    """
    stored = user_settings_service.get_gmail_config(db, user.id).get(
        "gmail_credentials_json"
    )
    try:
        credentials = user_settings_service.decrypt_gmail_credentials(stored)
    except SecretsError:
        credentials = None  # undecryptable: still disconnect locally
    if credentials:
        revoke_token(credentials)
    user_settings_service.clear_gmail_connection(db, user.id)
    # The mirrored mailbox and any running index/import belong to this grant; a
    # different account may be connected next.
    gmail_index_service.clear_index(db, user.id)
    try:
        for kind in ("index", "import"):
            gmail_runs.cancel_run(kind, user.id)
    except gmail_runs.RunStateUnavailable:
        # Disconnecting must work without Redis. With the credentials gone any
        # still-running hop fails at its next Gmail call and ends its own run.
        logger.warning("Could not cancel Gmail runs on disconnect (Redis unavailable)")
    _audit(db, user, "disconnect")
    db.commit()
    return _view(db, user)
