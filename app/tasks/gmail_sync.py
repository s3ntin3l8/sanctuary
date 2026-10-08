import contextlib
import logging
import time
import uuid
from collections.abc import Generator
from datetime import UTC, datetime, timedelta

import redis
from sqlalchemy.exc import SQLAlchemyError
from sqlalchemy.orm import Session

from app.config import REDIS_URL
from app.core.secrets import SecretsError
from app.models.database import GmailMessageIndex, UserSettings
from app.services import (
    gmail_cache,
    gmail_index_service,
    gmail_runs,
    user_settings_service,
)
from app.services.ingestion.batch_orchestrator import ingest_raw_email
from app.services.ingestion.gmail import (
    GmailReconnectRequired,
    build_query,
    connect_gmail,
    fetch_metadata,
    fetch_raw_message,
    has_filter,
    list_message_ids,
    parse_metadata,
)
from app.services.pipeline_status import batch_pipeline_settled as batch_is_settled
from app.tasks.celery_app import celery_app

logger = logging.getLogger(__name__)

# The watermark is taken at run *start*, not run end, and backed off by this
# much extra — see the comment in sync_gmail_for_user. Re-fetched messages in
# the overlap are cheap no-ops: ingest_raw_email dedups by Message-ID before
# doing any real work.
_WATERMARK_OVERLAP = timedelta(minutes=5)

# Prevents an incremental sync, an index refresh or an import hop (or two overlapping incremental
# ticks) for the same mailbox from racing: both would read the same
# gmail_last_sync_at, both fetch overlapping pages, and whichever commits its
# settings_json last silently clobbers the other's watermark update. TTL is
# the crash-recovery backstop, not the primary release path.
_LOCK_PREFIX = "sanctuary:gmail_sync_lock:"
# Crash-recovery backstop, not a hard guarantee: a full-mailbox index of a very
# large mailbox could outlive it, in which case a concurrent sync/import may
# interleave — harmless, since ingest dedups by Message-ID and the index upserts
# with ON CONFLICT DO NOTHING.
_LOCK_TTL_SECONDS = 60 * 60

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

# Index refresh commits and reports progress every this many messages.
_INDEX_CHUNK = 200

# Sequential import: how long to wait between "has the last email's pipeline
# finished?" checks, the gap before the next email when there was nothing to
# wait for, and the cap after which a stuck batch no longer holds the history.
_IMPORT_POLL_SECONDS = 15
_IMPORT_HOP_SECONDS = 2
_IMPORT_SETTLE_TIMEOUT = timedelta(minutes=30)

_lock_client: redis.Redis | None = None
_last_warn_at: float = 0.0
_WARN_INTERVAL = 60.0


def _get_user_settings(db: Session, user_id: int):
    """Return the per-user settings row (Gmail is connected per user)."""
    return db.query(UserSettings).filter(UserSettings.user_id == user_id).first()


def _raw_message(user_id: int, gmail_id: str, get_service) -> bytes:
    """The raw RFC822 message — from the local cache if we have it, else Gmail.

    ``get_service`` is only called on a cache miss, so a fully cached batch needs
    neither network nor a valid token. A fetched message is cached *before* the
    caller ingests it, so one that fails to ingest can be replayed once fixed.
    Nothing is cached when the fetch itself fails (including a dead grant) —
    there is no payload to keep.
    """
    cached = gmail_cache.read(user_id, gmail_id)
    if cached is not None:
        return cached
    raw = fetch_raw_message(get_service(), gmail_id)
    gmail_cache.write(user_id, gmail_id, raw)
    return raw


class _LazyService:
    """Builds the Gmail client the first time it is needed (see _raw_message)."""

    def __init__(self, connect):
        self._connect = connect
        self._service = None

    def __call__(self):
        if self._service is None:
            self._service = self._connect()
        return self._service


def _settings_json(db: Session, user_id: int) -> dict:
    settings = _get_user_settings(db, user_id)
    return (settings.settings_json or {}) if settings else {}


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
        # Unlike the history import, a dead grant is not re-raised per message
        # here: sync_gmail_for_user connects (and so detects a revoked token)
        # before it gets this far, and ends the run with "Reconnect required".
        try:
            raw_bytes = _raw_message(user_id, msg_id, lambda: service)
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
    """Best-effort mutex so only one sync (incremental, index refresh or import hop) runs per
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


def _public_error(exc: Exception) -> str:
    """Error text that is safe to show on Settings and the Import page.

    A database error's message carries the SQL statement and its bound
    parameters (which can include mail subjects/addresses); log those, don't
    surface them.
    """
    if isinstance(exc, SQLAlchemyError):
        return f"Database error ({type(exc).__name__}) — see the server log"
    return str(exc)[:500]


def _record_failure(
    db: Session, user_id: int, message: str, *, reconnect_required: bool = False
) -> None:
    """Persist why a run failed so Settings → Gmail can show it. Never raises:
    losing the status line must not mask the original failure."""
    try:
        db.rollback()
        user_settings_service.record_gmail_sync_outcome(
            db,
            user_id,
            error=message[:500],
            reconnect_required=reconnect_required,
        )
        db.commit()
    except Exception:
        db.rollback()
        logger.exception("Could not record Gmail sync failure for user %d", user_id)


def _ingest_query(
    db: Session, service, user_id: int, query: str
) -> tuple[int, list[str]]:
    """Page through ``query`` and ingest every hit. Returns (ok, failed ids)."""
    count = 0
    failed_ids: list[str] = []
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
            results = service.users().messages().list(userId="me", q=query).execute()

        messages = results.get("messages", [])
        page_ok, page_failed = _ingest_messages(
            db, service, user_id, [m["id"] for m in messages]
        )
        count += page_ok
        failed_ids.extend(page_failed)

        page_token = results.get("nextPageToken")
        if page_token:
            time.sleep(0.5)
    return count, failed_ids


def _cap_failures(failed_ids: list[str], user_id: int, source: str) -> list[str]:
    """Keep the most recently added failures when over the cap (callers append
    new failures after the older ones, so the oldest are dropped first)."""
    if len(failed_ids) <= _MAX_TRACKED_FAILURES:
        return failed_ids
    logger.warning(
        "%s: %d tracked failures for user %d exceeds the cap of %d — dropping "
        "the oldest %d (they will no longer be auto-retried)",
        source,
        len(failed_ids),
        user_id,
        _MAX_TRACKED_FAILURES,
        len(failed_ids) - _MAX_TRACKED_FAILURES,
    )
    return failed_ids[-_MAX_TRACKED_FAILURES:]


@celery_app.task(bind=True, max_retries=2)
def sync_gmail_incremental(self):
    """Beat entry point — fan out per mailbox, by the user's sync mode.

    ``auto`` users have new mail imported; ``notify`` users only have its headers
    indexed so it can be offered for import (``off`` users aren't touched and
    sync/check on demand).
    """
    from app.config import SessionLocal
    from app.tasks.dispatch import dispatch_task

    db = SessionLocal()
    try:
        by_mode = user_settings_service.gmail_users_by_mode(db)
    finally:
        db.close()

    for uid in by_mode["auto"]:
        dispatch_task(sync_gmail_for_user, uid)
    for uid in by_mode["notify"]:
        dispatch_task(check_gmail_new, uid)
    return (
        f"Dispatched Gmail sync for {len(by_mode['auto'])} and "
        f"new-mail check for {len(by_mode['notify'])} user(s)"
    )


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
        watermark = (run_started_at - _WATERMARK_OVERLAP).isoformat()

        from app.config import SessionLocal

        db = SessionLocal()
        try:
            settings = _get_user_settings(db, user_id)
            sj = (settings.settings_json or {}) if settings else {}
            if not sj.get("gmail_credentials_json"):
                return "Gmail not connected"

            allowlist = sj.get("gmail_allowlist", [])
            if not has_filter(allowlist, sj.get("gmail_label_filter")):
                return "No sender allowlist or label set"

            last_sync = sj.get("gmail_last_sync_at")
            if not last_sync:
                # Never run an unbounded query: with no watermark the search
                # would match the senders' entire history. Anchor here and let
                # the import page pull history deliberately.
                user_settings_service.reset_gmail_sync(db, user_id, since=watermark)
                db.commit()
                return "Initialized sync watermark"

            service = connect_gmail(db, user_id, sj)
            query = build_query(
                allowlist,
                sj.get("gmail_label_filter", ""),
                after=int(datetime.fromisoformat(last_sync).timestamp()),
            )

            # Retry messages that failed on a previous run first — tracked by
            # Gmail id regardless of whether they still fall inside the
            # watermark window. A message re-discovered here too (via the
            # normal windowed query below) is just a harmless duplicate
            # attempt; ingest_raw_email dedups by Message-ID.
            prior_failed_ids = list(sj.get("gmail_failed_message_ids") or [])
            retried_ok, still_failed_ids = _ingest_messages(
                db, service, user_id, prior_failed_ids
            )

            count, new_failed_ids = _ingest_query(db, service, user_id, query)

            # Watermark advances on every run regardless of failures — a
            # message that keeps failing must not be able to stall every
            # *other* message in the mailbox forever. It stays tracked in
            # gmail_failed_message_ids instead, so it's still retried (see
            # above) without gating anything else.
            #
            # A message can appear in both still_failed_ids (retried from
            # last run) and new_failed_ids (also inside this run's normal
            # window) if it keeps failing — dedupe (keeping first occurrence)
            # so one persistently-broken message doesn't eat two slots of
            # the tracked-failures cap for itself.
            failed_ids = _cap_failures(
                list(dict.fromkeys(still_failed_ids + new_failed_ids)),
                user_id,
                "Gmail sync",
            )

            total_ok = count + retried_ok
            result = f"Synced {total_ok} messages for user {user_id}"
            if failed_ids:
                result += f" ({len(failed_ids)} still failing, will retry)"

            # Re-read: a disconnect may have committed while we were fetching,
            # and writing our watermark/result back would resurrect half of it.
            db.refresh(settings)
            if not (settings.settings_json or {}).get("gmail_credentials_json"):
                return "Gmail disconnected during sync"

            new_json = dict(settings.settings_json or {})
            new_json["gmail_last_sync_at"] = watermark
            new_json["gmail_failed_message_ids"] = failed_ids
            new_json["gmail_last_sync_result"] = result
            new_json["gmail_last_sync_error"] = None
            new_json["gmail_reconnect_required"] = False
            settings.settings_json = new_json
            db.commit()
            return result
        except (GmailReconnectRequired, SecretsError) as e:
            # Only a fresh OAuth grant (or the right encryption key) fixes
            # these — retrying with backoff would just hammer Google.
            logger.warning("Gmail sync for user %d needs a reconnect: %s", user_id, e)
            _record_failure(db, user_id, str(e), reconnect_required=True)
            return "Reconnect required"
        except Exception as e:
            logger.error(f"Gmail incremental sync failed for user {user_id}: {e}")
            _record_failure(db, user_id, _public_error(e))
            raise
        finally:
            db.close()


def _index_chunk(db: Session, service, user_id: int, chunk: list[str]) -> int:
    """Fetch the headers of ``chunk`` and mirror them into the index (read-only
    against Gmail). Returns how many ids couldn't be indexed — Gmail didn't return
    them (quota, 5xx) or they didn't parse — so the next refresh retries them."""
    raws = fetch_metadata(service, chunk)
    skipped = len(set(chunk) - {raw["id"] for raw in raws})
    metas = []
    for raw in raws:
        try:
            metas.append(parse_metadata(raw))
        except (KeyError, ValueError, TypeError, AttributeError):
            # One malformed message must not lose the rest of the chunk.
            skipped += 1
            logger.warning("Gmail index: unparseable message %s", raw.get("id"))
    gmail_index_service.upsert_metadata(db, user_id, metas)
    db.commit()
    return skipped


@celery_app.task(
    bind=True, max_retries=3, autoretry_for=(Exception,), retry_backoff=True
)
def check_gmail_new(self, user_id: int):
    """Notify mode: index the headers of mail that arrived since the sync point so
    the app can offer it for import. Never ingests — importing stays a click.
    Read-only against Gmail (list + metadata get)."""
    with _user_sync_lock(user_id) as acquired:
        if not acquired:
            return (
                "Busy"  # a sync/index/import holds the mailbox; the next tick retries
            )

        checked_at = datetime.now(UTC)
        from app.config import SessionLocal

        db = SessionLocal()
        try:
            settings = _get_user_settings(db, user_id)
            sj = (settings.settings_json or {}) if settings else {}
            if not sj.get("gmail_credentials_json"):
                return "Gmail not connected"
            allowlist = sj.get("gmail_allowlist", [])
            if not has_filter(allowlist, sj.get("gmail_label_filter")):
                return "No sender allowlist or label set"
            last_sync = sj.get("gmail_last_sync_at")
            if not last_sync:
                user_settings_service.reset_gmail_sync(
                    db, user_id, since=checked_at.isoformat()
                )
                db.commit()
                return "Initialized sync watermark"

            service = connect_gmail(db, user_id, sj)
            query = build_query(
                allowlist,
                sj.get("gmail_label_filter", ""),
                after=int(datetime.fromisoformat(last_sync).timestamp()),
            )
            known = gmail_index_service.indexed_gmail_ids(db, user_id)
            new_ids = [i for i in list_message_ids(service, query) if i not in known]
            skipped = 0
            for start in range(0, len(new_ids), _INDEX_CHUNK):
                skipped += _index_chunk(
                    db, service, user_id, new_ids[start : start + _INDEX_CHUNK]
                )
            gmail_index_service.assign_group_keys(db, user_id)
            user_settings_service.record_gmail_check(
                db, user_id, checked_at.isoformat()
            )
            db.commit()
            return (
                f"Checked: {len(new_ids) - skipped} new message(s) for user {user_id}"
            )
        except (GmailReconnectRequired, SecretsError) as e:
            logger.warning("Gmail check for user %d needs a reconnect: %s", user_id, e)
            _record_failure(db, user_id, str(e), reconnect_required=True)
            return "Reconnect required"
        except Exception as e:
            logger.error("Gmail new-mail check failed for user %d: %s", user_id, e)
            _record_failure(db, user_id, _public_error(e))
            raise
        finally:
            db.close()


@celery_app.task(
    bind=True, max_retries=3, autoretry_for=(Exception,), retry_backoff=True
)
def index_gmail_mailbox(self, user_id: int, run_id: str):
    """Mirror the headers of every allowlisted message (all time) into
    ``gmail_message_index`` so the import page can group and order the whole
    history without ingesting any of it. Incremental: only ids not yet indexed
    are fetched. Read-only against Gmail (list + metadata get)."""
    with _user_sync_lock(user_id) as acquired:
        if not acquired:
            # User-triggered: defer rather than silently doing nothing.
            raise self.retry(countdown=30)

        from app.config import SessionLocal

        db = SessionLocal()
        try:
            settings = _get_user_settings(db, user_id)
            sj = (settings.settings_json or {}) if settings else {}
            allowlist = sj.get("gmail_allowlist", [])
            if not sj.get("gmail_credentials_json") or not has_filter(
                allowlist, sj.get("gmail_label_filter")
            ):
                gmail_runs.finish_run(
                    "index",
                    user_id,
                    run_id,
                    error="Connect Gmail and set a sender allowlist or a label first.",
                )
                return "Not configured"

            service = connect_gmail(db, user_id, sj)
            query = build_query(allowlist, sj.get("gmail_label_filter", ""))
            known = gmail_index_service.indexed_gmail_ids(db, user_id)
            new_ids = [i for i in list_message_ids(service, query) if i not in known]
            # A retry starts from a clean slate: clears the previous attempt's error.
            progress = {"total": len(new_ids), "done": 0, "skipped": 0, "error": None}
            if gmail_runs.update_run("index", user_id, run_id, progress) is None:
                return "Index superseded"

            skipped = 0
            for start in range(0, len(new_ids), _INDEX_CHUNK):
                chunk = new_ids[start : start + _INDEX_CHUNK]
                skipped += _index_chunk(db, service, user_id, chunk)
                progress = {"done": start + len(chunk), "skipped": skipped}
                if gmail_runs.update_run("index", user_id, run_id, progress) is None:
                    return "Index superseded"

            gmail_index_service.assign_group_keys(db, user_id)
            db.commit()
            gmail_runs.finish_run("index", user_id, run_id)
            return f"Indexed {len(new_ids) - skipped} new messages for user {user_id}"
        except (GmailReconnectRequired, SecretsError) as e:
            logger.warning("Gmail index for user %d needs a reconnect: %s", user_id, e)
            _record_failure(db, user_id, str(e), reconnect_required=True)
            gmail_runs.finish_run(
                "index", user_id, run_id, error=f"Reconnect required — {e}"
            )
            return "Reconnect required"
        except Exception as e:
            logger.error("Gmail index failed for user %d: %s", user_id, e)
            message = _public_error(e)
            if self.request.retries >= self.max_retries:
                gmail_runs.finish_run("index", user_id, run_id, error=message)
            else:
                # Celery retries this: keep the run active (so Refresh stays
                # disabled) and just show what went wrong.
                gmail_runs.update_run("index", user_id, run_id, {"error": message})
            raise
        finally:
            db.close()


def _import_label(db: Session, user_id: int, gmail_id: str) -> str | None:
    return (
        db.query(GmailMessageIndex.subject)
        .filter(
            GmailMessageIndex.owner_id == user_id,
            GmailMessageIndex.gmail_id == gmail_id,
        )
        .scalar()
    )


def _merge_import_outcome(
    db: Session, user_id: int, state: dict, *, record_status: bool = True
) -> None:
    """Fold a finished import into the user's sync status: failed ids join the
    list the incremental sync retries; the result line shows on Settings.

    ``record_status=False`` (an import that died on an error) only merges the
    failed ids — it must not overwrite the error/reconnect state just recorded.
    """
    settings = _get_user_settings(db, user_id)
    if settings is None:
        return
    data = dict(settings.settings_json or {})
    failed = list(state.get("failed") or [])
    if failed:
        data["gmail_failed_message_ids"] = _cap_failures(
            list(
                dict.fromkeys(list(data.get("gmail_failed_message_ids") or []) + failed)
            ),
            user_id,
            "Gmail import",
        )
    if not record_status:
        settings.settings_json = data
        db.commit()
        return
    verb = "Cancelled after" if state.get("cancelled") else "Imported"
    result = f"{verb} {state.get('done', 0)} of {state.get('total', 0)} messages"
    if failed:
        result += f" ({len(failed)} failed, will retry)"
    data["gmail_last_sync_result"] = result
    data["gmail_last_sync_error"] = None
    data["gmail_reconnect_required"] = False
    settings.settings_json = data
    db.commit()


def _import_one(
    db: Session, get_service, user_id: int, work: dict, gmail_id: str
) -> None:
    """Ingest one message, updating ``work`` in place."""
    work["current"] = {
        "gmail_id": gmail_id,
        "subject": _import_label(db, user_id, gmail_id),
    }
    try:
        batch = ingest_raw_email(
            db, _raw_message(user_id, gmail_id, get_service), owner_id=user_id
        )
        work["current"]["batch_id"] = batch.id if batch else None
    except (GmailReconnectRequired, SecretsError):
        raise  # not this message's fault: end the run and ask for a reconnect
    except Exception:
        db.rollback()
        work["failed"].append(gmail_id)
        work["current"]["batch_id"] = None
        logger.exception(
            "Gmail import: failed to ingest %s for user %d", gmail_id, user_id
        )
    work["done"] += 1


def _progress(work: dict) -> dict:
    return {key: work[key] for key in ("done", "remaining", "failed", "current")}


@celery_app.task(bind=True, max_retries=0)
def import_gmail_messages(self, user_id: int, run_id: str, hop: int = 0):
    """Ingest the queued messages, oldest first.

    The queue lives in the run state (``gmail_runs``), so cancelling is just
    flipping that state. In sequential mode the task ingests ONE message per
    hop and re-enqueues itself with a countdown until that email's documents
    have finished processing — earlier letters are fully enriched before their
    replies arrive — without ever blocking a worker while it waits.

    Every state write is atomic and keyed on ``run_id``; sequential hops are also
    fenced by ``hop`` (each re-enqueue carries the next number), so a stale,
    cancelled, redelivered or duplicated hop stops instead of clobbering anything.
    """
    from app.config import SessionLocal

    def _again(delay: int) -> str:
        self.apply_async(args=[user_id, run_id, hop + 1], countdown=delay)
        return "waiting"

    def _current() -> dict | None:
        state = gmail_runs.get_run("import", user_id)
        if (
            gmail_runs.is_active(state)
            and state["run_id"] == run_id  # type: ignore[index]
            and state.get("hop") == hop  # type: ignore[union-attr]
        ):
            return state
        return None

    def _stopped(work: dict) -> str:
        """A write found the run cancelled/replaced. For a cancel, still record
        what this hop got done (incl. failures) so the sync retries them."""
        latest = gmail_runs.get_run("import", user_id)
        if latest and latest["run_id"] == run_id:
            _merge_import_outcome(
                db,
                user_id,
                {
                    **latest,
                    "done": max(latest["done"], work["done"]),
                    "failed": list(dict.fromkeys(latest["failed"] + work["failed"])),
                },
            )
        return "Import cancelled"

    state = _current()
    if state is None:
        return "Import no longer active"
    sequential = state["sequential"]
    work = {
        **state,
        "remaining": list(state["remaining"]),
        "failed": list(state["failed"]),
    }

    db = SessionLocal()
    try:
        if (
            sequential
            and state.get("waiting_on")
            and not batch_is_settled(db, state["waiting_on"])
        ):
            waited = datetime.now(UTC) - datetime.fromisoformat(state["waiting_since"])
            if waited < _IMPORT_SETTLE_TIMEOUT:
                # Heartbeat + advance the fence, so the run isn't mistaken for
                # abandoned while it waits.
                if (
                    gmail_runs.update_run(
                        "import", user_id, run_id, {}, hop=hop, next_hop=hop + 1
                    )
                    is None
                ):
                    return _stopped(work)
                return _again(_IMPORT_POLL_SECONDS)
            logger.warning(
                "Gmail import for user %d: batch %s did not settle in %s — moving on",
                user_id,
                state["waiting_on"],
                _IMPORT_SETTLE_TIMEOUT,
            )

        with _user_sync_lock(user_id) as acquired:
            if not acquired:
                if (
                    gmail_runs.update_run(
                        "import", user_id, run_id, {}, hop=hop, next_hop=hop + 1
                    )
                    is None
                ):
                    return _stopped(work)
                return _again(_IMPORT_POLL_SECONDS)

            # Re-check under the lock: fences out a duplicated/redelivered chain.
            state = _current()
            if state is None:
                return "Import no longer active"
            work = {
                **state,
                "remaining": list(state["remaining"]),
                "failed": list(state["failed"]),
                "waiting_on": None,
            }
            # Built lazily: messages already in the local cache need no Gmail
            # connection (or valid token) at all.
            get_service = _LazyService(
                lambda: connect_gmail(db, user_id, _settings_json(db, user_id))
            )
            while work["remaining"]:
                _import_one(db, get_service, user_id, work, work["remaining"].pop(0))
                if sequential:
                    break
                if (
                    gmail_runs.update_run(
                        "import", user_id, run_id, _progress(work), hop=hop
                    )
                    is None
                ):
                    return _stopped(work)

        if work["remaining"]:
            batch_id = work["current"].get("batch_id")
            advanced = gmail_runs.update_run(
                "import",
                user_id,
                run_id,
                {
                    **_progress(work),
                    "waiting_on": batch_id,
                    "waiting_since": datetime.now(UTC).isoformat(),
                },
                hop=hop,
                next_hop=hop + 1,
            )
            if advanced is None:
                return _stopped(work)
            return _again(_IMPORT_POLL_SECONDS if batch_id else _IMPORT_HOP_SECONDS)

        finished = gmail_runs.finish_run(
            "import", user_id, run_id, patch={**_progress(work), "waiting_on": None}
        )
        if finished is None:
            return _stopped(work)
        _merge_import_outcome(db, user_id, finished)
        return f"Imported {finished['done']} of {finished['total']} messages"
    except (GmailReconnectRequired, SecretsError) as e:
        logger.warning("Gmail import for user %d needs a reconnect: %s", user_id, e)
        _record_failure(db, user_id, str(e), reconnect_required=True)
        finished = gmail_runs.finish_run(
            "import",
            user_id,
            run_id,
            patch=_progress(work),
            error=f"Reconnect required — {e}",
        )
        if finished:
            _merge_import_outcome(db, user_id, finished, record_status=False)
        return "Reconnect required"
    except Exception as e:
        logger.error("Gmail import failed for user %d: %s", user_id, e)
        finished = gmail_runs.finish_run(
            "import", user_id, run_id, patch=_progress(work), error=_public_error(e)
        )
        if finished:
            _merge_import_outcome(db, user_id, finished, record_status=False)
        raise
    finally:
        db.close()
