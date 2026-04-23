import os

from celery import Celery

REDIS_URL = os.getenv("REDIS_URL", "redis://localhost:6379/0")

celery_app = Celery(
    "sanctuary",
    broker=REDIS_URL,
    backend=REDIS_URL,
    include=[
        "app.tasks.document_processing",
        "app.tasks.gmail_sync",
        "app.tasks.analyze_batch",
        "app.tasks.enrich_document",
        "app.tasks.detect_relationships",
        "app.tasks.extract_claims",
        "app.tasks.extract_entities",
        "app.tasks.generate_embedding",
        "app.tasks.thread_open_scan",
        "app.tasks.scan_ingest",
        "app.tasks.prepare_slicing",
        "app.tasks.generate_case_brief",
        "app.tasks.maintenance",
    ],
)

from app.config import SCAN_POLL_INTERVAL_SECONDS

celery_app.conf.beat_schedule = {
    "sync-gmail-every-5-minutes": {
        "task": "app.tasks.gmail_sync.sync_gmail_incremental",
        "schedule": 300.0,
    },
    "close-threads-every-15-minutes": {
        "task": "app.tasks.thread_open_scan.thread_open_scan_task",
        "schedule": 900.0,
    },
    "scan-folder-polling": {
        "task": "app.tasks.scan_ingest.scan_folder_tick_task",
        "schedule": SCAN_POLL_INTERVAL_SECONDS,
    },
    "prune-ai-debug-logs-daily": {
        "task": "app.tasks.maintenance.prune_ai_debug_logs_task",
        "schedule": 86400.0,  # daily
    },
}

celery_app.conf.update(
    task_serializer="json",
    accept_content=["json"],
    result_serializer="json",
    timezone="UTC",
    enable_utc=True,
    task_track_started=True,
    task_time_limit=3600,
    worker_prefetch_multiplier=1,
    task_acks_late=True,
    task_reject_on_worker_lost=True,
    task_always_eager=os.getenv("CELERY_TASK_ALWAYS_EAGER", "true").lower() == "true",
)
