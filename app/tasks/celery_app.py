import logging
import os
from logging.handlers import RotatingFileHandler
from pathlib import Path

from celery import Celery
from celery.signals import after_setup_logger, after_setup_task_logger


def _suppress_httpx_noise(logger, **kwargs):
    log_level_str = os.getenv("LOG_LEVEL", "INFO").upper()
    if log_level_str != "DEBUG":
        logging.getLogger("httpx").setLevel(logging.WARNING)


def _setup_worker_logging(logger, loglevel, **kwargs):
    from app.core.log_formatter import LocalTimeFormatter

    fmt = LocalTimeFormatter("%(asctime)s | [%(levelname)s] %(name)s: %(message)s")
    root = logging.getLogger()

    # Re-format handlers Celery already installed (e.g. its hijacked StreamHandler).
    for h in root.handlers:
        h.setFormatter(fmt)

    if os.getenv("SANCTUARY_LOG_FILE", "1") == "0":
        return
    # Guard against duplicate registration (beat scheduler fires this signal too).
    if any(
        isinstance(h, RotatingFileHandler)
        and getattr(h, "baseFilename", "").endswith("celery.log")
        for h in root.handlers
    ):
        return
    log_dir = Path("scratch")
    log_dir.mkdir(exist_ok=True)
    fh = RotatingFileHandler(
        log_dir / "celery.log",
        maxBytes=10 * 1024 * 1024,  # 10 MB
        backupCount=5,
    )
    fh.setLevel(loglevel)
    fh.setFormatter(fmt)
    root.addHandler(fh)


after_setup_logger.connect(_suppress_httpx_noise)
after_setup_logger.connect(_setup_worker_logging)
after_setup_task_logger.connect(_suppress_httpx_noise)

from app.config import REDIS_URL, SCAN_POLL_INTERVAL_SECONDS

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
        "app.tasks.claim_dedup",
        "app.tasks.thread_open_scan",
        "app.tasks.scan_ingest",
        "app.tasks.prepare_slicing",
        "app.tasks.generate_case_brief",
        "app.tasks.maintenance",
    ],
)

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
    "recover-pipeline-hourly": {
        "task": "app.tasks.maintenance.recover_pipeline_task",
        "schedule": 3600.0,  # hourly
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
    task_always_eager=os.getenv("CELERY_TASK_ALWAYS_EAGER", "false").lower() == "true",
    # Propagate exceptions from eagerly-executed tasks so cascade failures are
    # visible in logs rather than silently captured in EagerResult. Has no effect
    # when task_always_eager is False.
    task_eager_propagates=os.getenv("CELERY_TASK_ALWAYS_EAGER", "false").lower()
    == "true",
    # Two-queue split: heavy Docling/Tesseract OCR is pinned to the `ingest`
    # queue (concurrency=1), everything else (LLM calls, embeddings, light I/O)
    # lands on `ai` (concurrency=2 to match LMStudio's two-slot capacity).
    task_default_queue="ai",
    task_routes={
        "app.tasks.document_processing.process_document_task": {"queue": "ingest"},
    },
)
