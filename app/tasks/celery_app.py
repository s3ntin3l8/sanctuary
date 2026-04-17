import os

from celery import Celery

REDIS_URL = os.getenv("REDIS_URL", "redis://localhost:6379/0")

celery_app = Celery(
    "sanctuary",
    broker=REDIS_URL,
    backend=REDIS_URL,
    include=[
        "app.tasks.document_processing",
        "app.tasks.ai_summaries",
        "app.tasks.gmail_sync",
    ],
)

celery_app.conf.beat_schedule = {
    "sync-gmail-every-5-minutes": {
        "task": "app.tasks.gmail_sync.sync_gmail_incremental",
        "schedule": 300.0,
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
)
