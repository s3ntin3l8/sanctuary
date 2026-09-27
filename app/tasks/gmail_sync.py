import contextlib
import logging
import time
import uuid
from collections.abc import Generator
from datetime import UTC, datetime, timedelta

import redis
from sqlalchemy.orm import Session

from app.config import REDIS_URL
from app.models.database import UserSettings
from app.services.ingestion.batch_orchestrator import ingest_raw_email
from app.services.ingestion.gmail import fetch_raw_message, get_gmail_service
from app.services.user_settings_service import user_ids_with_gmail
from app.tasks.celery_app import celery_app

logger = logging.getLogger(__name__)

# The watermark is taken at run *start*, not run end, and backed off by this
# much extra — see the comment in sync_gmail_for_user. Re-fetched messages in
# the overlap are cheap no-ops: ingest_raw_email dedups by Message-ID before
# doing any real work.
_WATERMARK_OVERLAP = timedelta(minutes=5)

# Prevents an incremental sync and a backfill (or two overlapping incremental
# ticks) for the same mailbox from racing: both would read the same
# gmail_last_sync_at, both fetch overlapping pages, and whichever commits its
# settings_json last silently clobbers the other's watermark update. TTL is
# the crash-recovery backstop, not the primary release path.
_LOCK_PREFIX = "sanctuary:gmail_sync_lock:"
_LOCK_TTL_SECONDS = 30 * 60  # 30 min — generous enough for a large backfill

_lock_client: redis.Redis | None = None
_last_warn_at: float = 0.0
_WARN_INTERVAL = 60.0


def _get_user_settings(db: Session, user_id: int):
    """Return the per-user settings row (Gmail is connected per user)."""
    return db.query(UserSettings).filter(UserSettings.user_id == user_id).first()


def _get_lock_client() -> redis.Redis:
    global _lock_client
    if _lock_client is None:
        _lock_client = redis.Redis.from_url(
            REDIS_URL,
            socket_timeout=1.0,
            socket_connect_timeout=1.0,
            decode_responses=True,
        )
    return _lock_client


def _maybe_warn(exc: Exception) -> None:
    global _last_warn_at
    now = time.monotonic()
    if now - _last_warn_at >= _WARN_INTERVAL:
        logger.warning(
            "gmail_sync: Redis unavailable (%s); proceeding without the "
            "per-user sync lock",
            exc,
        )
        _last_warn_at = now


@contextlib.contextmanager
def _user_sync_lock(user_id: int) -> Generator[bool, None, None]:
    """Best-effort mutex so only one sync (incremental or backfill) runs per
    mailbox at a time.

    Yields True if the lock was acquired (or Redis is unavailable — degrades
    open, same convention as model_gate/ai_inflight: losing the lock risks a
    rare duplicate fetch, not a correctness gate, since Message-ID dedup in
    ingest_raw_email already makes a re-fetch of the same message idempotent).
    Yields False if another sync for this user currently holds it — the
    caller should skip this run rather than proceed unprotected.
    """
    key = f"{_LOCK_PREFIX}{user_id}"
    token = uuid.uuid4().hex
    held = False
    try:
        held = bool(_get_lock_client().set(key, token, nx=True, ex=_LOCK_TTL_SECONDS))
    except (redis.RedisError, OSError) as exc:
        _maybe_warn(exc)
        yield True
        return

    if not held:
        yield False
        return

    try:
        yield True
    finally:
        try:
            client = _get_lock_client()
            # Only release if we still hold it — a lock whose TTL already
            # expired and was re-acquired by someone else must not be deleted
            # out from under them.
            if client.get(key) == token:
                client.delete(key)
        except (redis.RedisError, OSError) as exc:
            _maybe_warn(exc)


@celery_app.task(bind=True, max_retries=2)
def sync_gmail_incremental(self):
    """Beat entry point — fan out one incremental sync per connected mailbox.

    Each connected user gets their own per-user sync; ingested emails are owned
    by that user (their triage inbox).
    """
    from app.config import SessionLocal
    from app.tasks.dispatch import dispatch_task

    db = SessionLocal()
    try:
        user_ids = user_ids_with_gmail(db)
    finally:
        db.close()

    for uid in user_ids:
        dispatch_task(sync_gmail_for_user, uid)
    return f"Dispatched Gmail sync for {len(user_ids)} user(s)"


@celery_app.task(
    bind=True, max_retries=5, autoretry_for=(Exception,), retry_backoff=True
)
def sync_gmail_for_user(self, user_id: int):
    with _user_sync_lock(user_id) as acquired:
        if not acquired:
            logger.info(
                "Gmail sync for user %d already in progress — skipping this tick",
                user_id,
            )
            return "Sync already in progress"

        # Watermark is the run's *start* time, not its end. Anything that
        # arrives in the mailbox while this run is still paginating would be
        # silently skipped by the next run if we watermarked at the end (its
        # "after:" filter would already be past that message's date). The
        # overlap is extra slack on top of that for clock skew between this
        # host and Gmail's own timestamps.
        run_started_at = datetime.now(UTC)

        from app.config import SessionLocal

        db = SessionLocal()
        try:
            settings = _get_user_settings(db, user_id)
            sj = (settings.settings_json or {}) if settings else {}
            if not sj.get("gmail_credentials_json"):
                return "Gmail not connected"

            service = get_gmail_service(sj["gmail_credentials_json"])

            allowlist = sj.get("gmail_allowlist", [])
            if not allowlist:
                return "Allowlist empty"

            label_filter = sj.get("gmail_label_filter", "")

            from_q = " OR ".join([f"from:{e}" for e in allowlist])
            query = f"({from_q})"
            if label_filter:
                query += f" label:{label_filter}"

            last_sync = sj.get("gmail_last_sync_at")
            if last_sync:
                dt = datetime.fromisoformat(last_sync)
                query += f" after:{int(dt.timestamp())}"

            count = 0
            failed = 0
            page_token = None
            first_page = True

            while first_page or page_token:
                first_page = False
                if page_token:
                    results = (
                        service.users()
                        .messages()
                        .list(userId="me", q=query, pageToken=page_token)
                        .execute()
                    )
                else:
                    results = (
                        service.users().messages().list(userId="me", q=query).execute()
                    )

                messages = results.get("messages", [])
                for msg in messages:
                    # One bad message (malformed raw bytes, a transient parse
                    # error, ...) must not abort the whole run — that would
                    # leave the watermark unadvanced and re-fetch every good
                    # message in the window on every future tick too.
                    try:
                        raw_bytes = fetch_raw_message(service, msg["id"])
                        ingest_raw_email(db, raw_bytes, owner_id=user_id)
                        count += 1
                    except Exception:
                        db.rollback()
                        failed += 1
                        logger.exception(
                            "Gmail sync: failed to ingest message %s for "
                            "user %d — skipping it",
                            msg.get("id"),
                            user_id,
                        )

                page_token = results.get("nextPageToken")
                if page_token:
                    time.sleep(0.5)

            new_json = dict(settings.settings_json or {})
            new_json["gmail_last_sync_at"] = (
                run_started_at - _WATERMARK_OVERLAP
            ).isoformat()
            settings.settings_json = new_json
            db.commit()

            result = f"Synced {count} messages for user {user_id}"
            if failed:
                result += f" ({failed} failed and were skipped)"
            return result
        except Exception as e:
            logger.error(f"Gmail incremental sync failed for user {user_id}: {e}")
            raise
        finally:
            db.close()


@celery_app.task(
    bind=True, max_retries=3, autoretry_for=(Exception,), retry_backoff=True
)
def run_gmail_backfill(self, user_id: int, days: int = 90):
    with _user_sync_lock(user_id) as acquired:
        if not acquired:
            logger.info(
                "Gmail backfill for user %d skipped — a sync is already in "
                "progress for this mailbox",
                user_id,
            )
            return "Sync already in progress"

        from app.config import SessionLocal

        db = SessionLocal()
        try:
            settings = _get_user_settings(db, user_id)
            sj = (settings.settings_json or {}) if settings else {}
            if not sj.get("gmail_credentials_json"):
                return "Gmail not connected"

            service = get_gmail_service(sj["gmail_credentials_json"])
            allowlist = sj.get("gmail_allowlist", [])
            if not allowlist:
                return "Allowlist empty"

            from_q = " OR ".join([f"from:{e}" for e in allowlist])
            cutoff_date = datetime.now(UTC) - timedelta(days=days)
            query = f"({from_q}) after:{int(cutoff_date.timestamp())}"

            count = 0
            failed = 0
            page_token = None
            first_page = True

            while first_page or page_token:
                first_page = False
                if page_token:
                    results = (
                        service.users()
                        .messages()
                        .list(userId="me", q=query, pageToken=page_token)
                        .execute()
                    )
                else:
                    results = (
                        service.users().messages().list(userId="me", q=query).execute()
                    )

                messages = results.get("messages", [])
                for msg in messages:
                    try:
                        raw_bytes = fetch_raw_message(service, msg["id"])
                        ingest_raw_email(db, raw_bytes, owner_id=user_id)
                        count += 1
                    except Exception:
                        db.rollback()
                        failed += 1
                        logger.exception(
                            "Gmail backfill: failed to ingest message %s for "
                            "user %d — skipping it",
                            msg.get("id"),
                            user_id,
                        )

                page_token = results.get("nextPageToken")
                if page_token:
                    time.sleep(0.5)

            result = f"Backfilled {count} messages for user {user_id}"
            if failed:
                result += f" ({failed} failed and were skipped)"
            return result
        except Exception as e:
            logger.error(f"Gmail backfill failed for user {user_id}: {e}")
            raise
        finally:
            db.close()
