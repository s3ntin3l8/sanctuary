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

# A message that fails to fetch/ingest is tracked here (by Gmail's own
# message id, not the RFC822 Message-ID header) instead of just being logged
# and forgotten. Without this, advancing the watermark past a run with
# failures would silently and permanently drop that mail — the next run's
# `after:` filter is already past it. Retried at the start of every future
# run regardless of the watermark window; dropped once it succeeds. Capped so
# one mailbox that's stuck failing forever can't grow this list unboundedly —
# the oldest untracked failures are traded away with a log line rather than
# retried forever.
_MAX_TRACKED_FAILURES = 200

_lock_client: redis.Redis | None = None
_last_warn_at: float = 0.0
_WARN_INTERVAL = 60.0


def _get_user_settings(db: Session, user_id: int):
    """Return the per-user settings row (Gmail is connected per user)."""
    return db.query(UserSettings).filter(UserSettings.user_id == user_id).first()


def _ingest_messages(
    db: Session, service, user_id: int, message_ids: list[str]
) -> tuple[int, list[str]]:
    """Fetch and ingest each Gmail message id, isolating per-message failures
    so one bad message can't abort the run or block the others.

    Returns (succeeded_count, failed_message_ids).
    """
    succeeded = 0
    failed_ids: list[str] = []
    for msg_id in message_ids:
        try:
            raw_bytes = fetch_raw_message(service, msg_id)
            ingest_raw_email(db, raw_bytes, owner_id=user_id)
            succeeded += 1
        except Exception:
            db.rollback()
            failed_ids.append(msg_id)
            logger.exception(
                "Gmail sync: failed to ingest message %s for user %d — "
                "will retry on the next run",
                msg_id,
                user_id,
            )
    return succeeded, failed_ids


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


# Atomic compare-and-delete for lock release: a plain GET-then-DELETE is a
# TOCTOU race — if the TTL expires and someone else re-acquires the key
# between our GET and DELETE, a bare DELETE would remove *their* lock. Same
# pattern as model_gate.py's acquire script.
_RELEASE_LUA = """
if redis.call("GET", KEYS[1]) == ARGV[1] then
    return redis.call("DEL", KEYS[1])
else
    return 0
end
"""

_release_script = None


def _get_release_script(client: redis.Redis):
    global _release_script
    if _release_script is None:
        _release_script = client.register_script(_RELEASE_LUA)
    return _release_script


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
            # Atomic compare-and-delete — only release if we still hold it.
            # A lock whose TTL already expired and was re-acquired by someone
            # else must not be deleted out from under them.
            _get_release_script(client)(keys=[key], args=[token])
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

            # Retry messages that failed on a previous run first — tracked by
            # Gmail id regardless of whether they still fall inside the
            # watermark window. A message re-discovered here too (via the
            # normal windowed query below) is just a harmless duplicate
            # attempt; ingest_raw_email dedups by Message-ID.
            prior_failed_ids = list(sj.get("gmail_failed_message_ids") or [])
            retried_ok, still_failed_ids = _ingest_messages(
                db, service, user_id, prior_failed_ids
            )

            count = 0
            new_failed_ids: list[str] = []
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
                page_ok, page_failed = _ingest_messages(
                    db, service, user_id, [m["id"] for m in messages]
                )
                count += page_ok
                new_failed_ids.extend(page_failed)

                page_token = results.get("nextPageToken")
                if page_token:
                    time.sleep(0.5)

            # Watermark advances on every run regardless of failures — a
            # message that keeps failing must not be able to stall every
            # *other* message in the mailbox forever. It stays tracked in
            # gmail_failed_message_ids instead, so it's still retried (see
            # the top of this function) without gating anything else.
            failed_ids = still_failed_ids + new_failed_ids
            if len(failed_ids) > _MAX_TRACKED_FAILURES:
                logger.warning(
                    "Gmail sync: %d tracked failures for user %d exceeds the "
                    "cap of %d — dropping the oldest %d (they will no longer "
                    "be auto-retried)",
                    len(failed_ids),
                    user_id,
                    _MAX_TRACKED_FAILURES,
                    len(failed_ids) - _MAX_TRACKED_FAILURES,
                )
                failed_ids = failed_ids[-_MAX_TRACKED_FAILURES:]

            new_json = dict(settings.settings_json or {})
            new_json["gmail_last_sync_at"] = (
                run_started_at - _WATERMARK_OVERLAP
            ).isoformat()
            new_json["gmail_failed_message_ids"] = failed_ids
            settings.settings_json = new_json
            db.commit()

            total_ok = count + retried_ok
            result = f"Synced {total_ok} messages for user {user_id}"
            if failed_ids:
                result += f" ({len(failed_ids)} still failing, will retry)"
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
            # This is a user-triggered action (the "connect Gmail" flow),
            # not a beat tick — silently no-op'ing here would leave the user
            # thinking their backfill ran when it never started. Defer and
            # retry instead; max_retries=3 at 30s apart bounds the wait.
            logger.info(
                "Gmail backfill for user %d deferred — a sync is already in "
                "progress for this mailbox, retrying shortly",
                user_id,
            )
            raise self.retry(countdown=30)

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
            new_failed_ids: list[str] = []
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
                page_ok, page_failed = _ingest_messages(
                    db, service, user_id, [m["id"] for m in messages]
                )
                count += page_ok
                new_failed_ids.extend(page_failed)

                page_token = results.get("nextPageToken")
                if page_token:
                    time.sleep(0.5)

            if new_failed_ids:
                # Share the same tracked-failures list incremental sync
                # drains on every run, rather than dropping backfill
                # failures on the floor — the next incremental tick (or
                # another backfill) will retry them.
                prior = list(sj.get("gmail_failed_message_ids") or [])
                merged = (prior + new_failed_ids)[-_MAX_TRACKED_FAILURES:]
                new_json = dict(settings.settings_json or {})
                new_json["gmail_failed_message_ids"] = merged
                settings.settings_json = new_json

            db.commit()

            result = f"Backfilled {count} messages for user {user_id}"
            if new_failed_ids:
                result += f" ({len(new_failed_ids)} failed, will retry)"
            return result
        except Exception as e:
            logger.error(f"Gmail backfill failed for user {user_id}: {e}")
            raise
        finally:
            db.close()
