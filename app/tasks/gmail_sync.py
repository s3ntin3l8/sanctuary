import logging
from datetime import UTC, datetime

from sqlalchemy.orm import Session

from app.config import SessionLocal
from app.models.database import UserSettings
from app.services.ingestion.batch_orchestrator import ingest_raw_email
from app.services.ingestion.gmail import fetch_raw_message, get_gmail_service
from app.tasks.celery_app import celery_app

logger = logging.getLogger(__name__)


def _get_user_settings(db: Session, user_id: str = "single_user"):
    return db.query(UserSettings).filter(UserSettings.user_id == user_id).first()


@celery_app.task
def sync_gmail_incremental():
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

        # Build query: (from:e1 OR from:e2)
        from_q = " OR ".join([f"from:{e}" for e in allowlist])
        query = f"({from_q})"
        if label_filter:
            query += f" label:{label_filter}"

        last_sync = settings.settings_json.get("gmail_last_sync_at")
        if last_sync:
            # Gmail query 'after' uses seconds since epoch or YYYY/MM/DD
            dt = datetime.fromisoformat(last_sync)
            query += f" after:{int(dt.timestamp())}"

        results = service.users().messages().list(userId="me", q=query).execute()
        messages = results.get("messages", [])

        count = 0
        for msg in messages:
            raw_bytes = fetch_raw_message(service, msg["id"])
            ingest_raw_email(db, raw_bytes)
            count += 1

        # Update last sync timestamp
        s_json = dict(settings.settings_json)
        s_json["gmail_last_sync_at"] = datetime.now(UTC).isoformat()
        settings.settings_json = s_json
        db.commit()

        return f"Synced {count} messages"
    except Exception as e:
        logger.error(f"Gmail incremental sync failed: {e}")
        return str(e)
    finally:
        db.close()


@celery_app.task
def run_gmail_backfill(user_id: str, days: int = 90):
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
        query = f"({from_q}) older_than:{days}d"

        # To be safe, let's use a simpler query for backfill: all from allowlist
        query = f"({from_q})"

        results = service.users().messages().list(userId="me", q=query).execute()
        messages = results.get("messages", [])

        count = 0
        for msg in messages:
            raw_bytes = fetch_raw_message(service, msg["id"])
            ingest_raw_email(db, raw_bytes)
            count += 1

        return f"Backfilled {count} messages"
    except Exception as e:
        logger.error(f"Gmail backfill failed: {e}")
        return str(e)
    finally:
        db.close()
