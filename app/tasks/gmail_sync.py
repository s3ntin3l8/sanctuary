import logging
import time
from datetime import UTC, datetime, timedelta

from sqlalchemy.orm import Session

from app.config import SessionLocal
from app.models.database import UserSettings
from app.services.ingestion.batch_orchestrator import ingest_raw_email
from app.services.ingestion.gmail import fetch_raw_message, get_gmail_service
from app.tasks.celery_app import celery_app

logger = logging.getLogger(__name__)


def _get_user_settings(db: Session, user_id: str = "single_user"):
    return db.query(UserSettings).filter(UserSettings.user_id == user_id).first()


@celery_app.task(
    bind=True, max_retries=5, autoretry_for=(Exception,), retry_backoff=True
)
def sync_gmail_incremental(self):
    db = SessionLocal()
    try:
        settings = _get_user_settings(db)
        if not settings or "gmail_credentials_json" not in settings.settings_json:
            return "Gmail not connected"

        service = get_gmail_service(settings.settings_json["gmail_credentials_json"])

        allowlist = settings.settings_json.get("gmail_allowlist", [])
        if not allowlist:
            return "Allowlist empty"

        label_filter = settings.settings_json.get("gmail_label_filter", "")

        from_q = " OR ".join([f"from:{e}" for e in allowlist])
        query = f"({from_q})"
        if label_filter:
            query += f" label:{label_filter}"

        last_sync = settings.settings_json.get("gmail_last_sync_at")
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
                ingest_raw_email(db, raw_bytes)
                count += 1

            page_token = results.get("nextPageToken")
            if page_token:
                time.sleep(0.5)

        s_json = dict(settings.settings_json)
        s_json["gmail_last_sync_at"] = datetime.now(UTC).isoformat()
        settings.settings_json = s_json
        db.commit()

        return f"Synced {count} messages"
    except Exception as e:
        logger.error(f"Gmail incremental sync failed: {e}")
        raise
    finally:
        db.close()


@celery_app.task(
    bind=True, max_retries=3, autoretry_for=(Exception,), retry_backoff=True
)
def run_gmail_backfill(self, user_id: str, days: int = 90):
    db = SessionLocal()
    try:
        settings = _get_user_settings(db, user_id)
        if not settings or "gmail_credentials_json" not in settings.settings_json:
            return "Gmail not connected"

        service = get_gmail_service(settings.settings_json["gmail_credentials_json"])
        allowlist = settings.settings_json.get("gmail_allowlist", [])
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
                ingest_raw_email(db, raw_bytes)
                count += 1

            page_token = results.get("nextPageToken")
            if page_token:
                time.sleep(0.5)

        return f"Backfilled {count} messages"
    except Exception as e:
        logger.error(f"Gmail backfill failed: {e}")
        raise
    finally:
        db.close()
