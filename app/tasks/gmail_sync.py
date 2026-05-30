import logging
import time
from datetime import UTC, datetime, timedelta

from sqlalchemy.orm import Session

from app.config import SessionLocal
from app.models.database import UserSettings
from app.services.ingestion.batch_orchestrator import ingest_raw_email
from app.services.ingestion.gmail import fetch_raw_message, get_gmail_service
from app.services.user_settings_service import user_ids_with_gmail
from app.tasks.celery_app import celery_app

logger = logging.getLogger(__name__)


def _get_user_settings(db: Session, user_id: int):
    """Return the per-user settings row (Gmail is connected per user)."""
    return db.query(UserSettings).filter(UserSettings.user_id == user_id).first()


@celery_app.task(bind=True, max_retries=2)
def sync_gmail_incremental(self):
    """Beat entry point — fan out one incremental sync per connected mailbox.

    Each connected user gets their own per-user sync; ingested emails are owned
    by that user (their triage inbox).
    """
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
                raw_bytes = fetch_raw_message(service, msg["id"])
                ingest_raw_email(db, raw_bytes, owner_id=user_id)
                count += 1

            page_token = results.get("nextPageToken")
            if page_token:
                time.sleep(0.5)

        new_json = dict(settings.settings_json or {})
        new_json["gmail_last_sync_at"] = datetime.now(UTC).isoformat()
        settings.settings_json = new_json
        db.commit()

        return f"Synced {count} messages for user {user_id}"
    except Exception as e:
        logger.error(f"Gmail incremental sync failed for user {user_id}: {e}")
        raise
    finally:
        db.close()


@celery_app.task(
    bind=True, max_retries=3, autoretry_for=(Exception,), retry_backoff=True
)
def run_gmail_backfill(self, user_id: int, days: int = 90):
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
                raw_bytes = fetch_raw_message(service, msg["id"])
                ingest_raw_email(db, raw_bytes, owner_id=user_id)
                count += 1

            page_token = results.get("nextPageToken")
            if page_token:
                time.sleep(0.5)

        return f"Backfilled {count} messages for user {user_id}"
    except Exception as e:
        logger.error(f"Gmail backfill failed for user {user_id}: {e}")
        raise
    finally:
        db.close()
