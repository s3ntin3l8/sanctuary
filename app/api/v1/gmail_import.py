"""Gmail history import: index the mailbox, browse it grouped by case reference,
and import chosen messages oldest first."""

from __future__ import annotations

import uuid
from datetime import UTC, datetime, time
from typing import cast

from fastapi import APIRouter, Depends, Query, Request, Response
from sqlalchemy.orm import Session

from app.api.v1.errors import ApiError
from app.core.rate_limit import limiter
from app.dependencies import get_current_user, get_db
from app.models.database import User
from app.schemas.gmail_import import (
    GmailGroup,
    GmailGroupList,
    GmailImportQueued,
    GmailImportRequest,
    GmailImportStatus,
    GmailIndexedMessage,
    GmailIndexStatus,
    GmailMessagePage,
    GmailNewMessages,
)
from app.schemas.settings import SyncMode
from app.services import (
    gmail_cache,
    gmail_import_status,
    gmail_index_service,
    gmail_runs,
    user_settings_service,
)
from app.services.gmail_runs import RunStateUnavailable
from app.services.ingestion.gmail import has_filter

router = APIRouter(prefix="/gmail", tags=["gmail"])


def _require_ready(db: Session, user: User) -> None:
    cfg = user_settings_service.get_gmail_config(db, user.id)
    if not cfg.get("gmail_credentials_json"):
        raise ApiError(409, "gmail_not_connected", "Connect Gmail first.")
    if not has_filter(cfg.get("gmail_allowlist"), cfg.get("gmail_label_filter")):
        raise ApiError(
            409, "gmail_filter_missing", "Set a sender allowlist or a label first."
        )


def _run_state_down() -> ApiError:
    return ApiError(
        503,
        "run_state_unavailable",
        "Redis is unreachable, so the run can't be tracked. Start Redis and retry.",
    )


@router.post("/index", status_code=202, response_class=Response)
@limiter.limit("6/minute")
def refresh_index(
    request: Request,
    db: Session = Depends(get_db),
    user: User = Depends(get_current_user),
):
    """Fetch headers of every allowlisted message not indexed yet (idempotent)."""
    from app.tasks.dispatch import dispatch_task
    from app.tasks.gmail_sync import index_gmail_mailbox

    _require_ready(db, user)
    run_id = uuid.uuid4().hex
    try:
        started = gmail_runs.begin_run(
            "index",
            user.id,
            {"run_id": run_id, "total": 0, "done": 0, "skipped": 0, "error": None},
        )
    except RunStateUnavailable as exc:
        raise _run_state_down() from exc
    if started:
        dispatch_task(index_gmail_mailbox, user.id, run_id)


@router.get("/index/status", response_model=GmailIndexStatus)
def index_status(db: Session = Depends(get_db), user: User = Depends(get_current_user)):
    state = gmail_runs.get_run("index", user.id) or {}
    count, last = gmail_index_service.index_summary(db, user.id)
    cached_count, cached_bytes = gmail_cache.stats(user.id)
    return GmailIndexStatus(
        running=gmail_runs.is_live(state),
        indexed_count=count,
        last_indexed_at=last,
        done=state.get("done", 0),
        total=state.get("total", 0),
        skipped=state.get("skipped", 0),
        error=state.get("error"),
        cached_count=cached_count,
        cached_bytes=cached_bytes,
    )


@router.get("/groups", response_model=GmailGroupList)
def groups(db: Session = Depends(get_db), user: User = Depends(get_current_user)):
    return GmailGroupList(
        groups=[GmailGroup(**g) for g in gmail_index_service.list_groups(db, user)]
    )


@router.get("/messages", response_model=GmailMessagePage)
def messages(
    group: str | None = None,
    cursor: str | None = None,
    limit: int = Query(100, ge=1, le=200),
    db: Session = Depends(get_db),
    user: User = Depends(get_current_user),
):
    try:
        rows, next_cursor = gmail_index_service.list_messages(
            db, user.id, group=group, cursor=cursor, limit=limit
        )
    except ValueError as exc:
        raise ApiError(422, "invalid_cursor", "Invalid page cursor.") from exc
    cached = gmail_cache.cached_ids(user.id, [row.gmail_id for row, _ in rows])
    return GmailMessagePage(
        items=[
            _indexed_message(row, ingested, row.gmail_id in cached)
            for row, ingested in rows
        ],
        next_cursor=next_cursor,
    )


def _indexed_message(row, ingested: bool, cached: bool) -> GmailIndexedMessage:
    return GmailIndexedMessage(
        gmail_id=row.gmail_id,
        thread_id=row.thread_id,
        sender=row.sender,
        subject=row.subject,
        sent_at=row.sent_at,
        has_attachments=row.has_attachments,
        ingested=ingested,
        cached=cached,
    )


def _sync_point(cfg: dict) -> datetime | None:
    value = cfg.get("gmail_last_sync_at")
    return datetime.fromisoformat(value) if value else None


def _new_view(db: Session, user: User) -> GmailNewMessages:
    cfg = user_settings_service.get_gmail_config(db, user.id)
    since = _sync_point(cfg) if cfg.get("gmail_credentials_json") else None
    checked = cfg.get("gmail_last_check_at")
    checked_at = datetime.fromisoformat(checked) if checked else None
    mode = cast(SyncMode, cfg.get("gmail_sync_mode") or "off")
    if since is None:
        return GmailNewMessages(
            count=0, sync_mode=mode, since=None, checked_at=checked_at, items=[]
        )
    count, rows = gmail_index_service.new_messages(db, user.id, since)
    cached = gmail_cache.cached_ids(user.id, [row.gmail_id for row in rows])
    return GmailNewMessages(
        count=count,
        sync_mode=mode,
        since=since,
        checked_at=checked_at,
        items=[_indexed_message(row, False, row.gmail_id in cached) for row in rows],
    )


@router.get("/new", response_model=GmailNewMessages)
def new_messages(db: Session = Depends(get_db), user: User = Depends(get_current_user)):
    """New mail awaiting a decision: arrived after the sync point, not imported."""
    return _new_view(db, user)


@router.post("/new/check", status_code=202, response_class=Response)
@limiter.limit("6/minute")
def check_new(
    request: Request,
    db: Session = Depends(get_db),
    user: User = Depends(get_current_user),
):
    """Look for new mail now (indexes headers only; imports nothing)."""
    from app.tasks.dispatch import dispatch_task
    from app.tasks.gmail_sync import check_gmail_new

    _require_ready(db, user)
    dispatch_task(check_gmail_new, user.id)


@router.post("/new/dismiss", response_model=GmailNewMessages)
@limiter.limit("12/minute")
def dismiss_new(
    request: Request,
    db: Session = Depends(get_db),
    user: User = Depends(get_current_user),
):
    """Skip the listed new mail: move the sync point to the newest of it.

    Mail that arrives (or is indexed) later is still new. Nothing is imported or
    deleted; the skipped mail stays available through Import history, and failed
    messages are still retried.
    """
    _require_ready(db, user)
    cfg = user_settings_service.get_gmail_config(db, user.id)
    since = _sync_point(cfg)
    if since is not None:
        newest = gmail_index_service.newest_new_arrival(db, user.id, since)
        if newest is not None:
            user_settings_service.set_gmail_sync_point(
                db, user.id, newest.replace(tzinfo=UTC).isoformat()
            )
            db.commit()
    return _new_view(db, user)


@router.post("/import", response_model=GmailImportQueued, status_code=202)
@limiter.limit("12/minute")
def start_import(
    request: Request,
    body: GmailImportRequest,
    db: Session = Depends(get_db),
    user: User = Depends(get_current_user),
):
    """Queue the not-yet-imported messages of the selection, oldest first."""
    from app.tasks.dispatch import dispatch_task
    from app.tasks.gmail_sync import import_gmail_messages

    _require_ready(db, user)
    since = None
    if body.new:
        since = _sync_point(user_settings_service.get_gmail_config(db, user.id))
        if since is None:
            return GmailImportQueued(queued=0)
    ids = gmail_index_service.select_for_import(
        db,
        user.id,
        since=since,
        gmail_ids=body.gmail_ids,
        group=body.group,
        oldest_n=body.oldest_n,
        before=(
            datetime.combine(body.before, time.min, tzinfo=UTC) if body.before else None
        ),
    )
    if not ids:
        return GmailImportQueued(queued=0)

    run_id = uuid.uuid4().hex
    try:
        started = _begin_import(user.id, run_id, ids, body.sequential)
    except RunStateUnavailable as exc:
        raise _run_state_down() from exc
    if started is None:
        raise ApiError(409, "import_running", "An import is already running.")
    dispatch_task(import_gmail_messages, user.id, run_id)
    return GmailImportQueued(queued=len(ids))


def _begin_import(user_id: int, run_id: str, ids: list[str], sequential: bool):
    return gmail_runs.begin_run(
        "import",
        user_id,
        {
            "run_id": run_id,
            "total": len(ids),
            "done": 0,
            "remaining": ids,
            "failed": [],
            "sequential": sequential,
            "waiting_on": None,
            "waiting_since": None,
            "current": None,
            "cancelled": False,
            "error": None,
        },
    )


@router.get("/import/status", response_model=GmailImportStatus)
def import_status(user: User = Depends(get_current_user)):
    return gmail_import_status.import_status(user.id)


@router.delete("/import", status_code=204, response_class=Response)
@limiter.limit("12/minute")
def cancel_import(request: Request, user: User = Depends(get_current_user)):
    """Stop after the message currently being ingested."""
    if not gmail_runs.cancel_run("import", user.id):
        raise ApiError(409, "no_import_running", "No import is running.")


@router.delete("/cache", status_code=204, response_class=Response)
@limiter.limit("6/minute")
def clear_cache(request: Request, user: User = Depends(get_current_user)):
    """Delete the local copies of fetched messages (local files only — nothing in
    Gmail or in already-imported bundles is touched)."""
    gmail_cache.clear(user.id)
