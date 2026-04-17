from app.tasks.celery_app import celery_app
import logging

logger = logging.getLogger(__name__)

@celery_app.task
def sync_gmail_incremental():
    logger.info("Running incremental Gmail sync...")
    # Full implementation requires OAuth setup in settings first
    pass
    
@celery_app.task
def run_gmail_backfill(user_id: int, days: int = 90):
    logger.info(f"Running Gmail backfill for past {days} days...")
    pass
