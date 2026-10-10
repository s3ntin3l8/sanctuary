import logging
import os
from logging.handlers import RotatingFileHandler
from pathlib import Path

from celery import Celery
from celery.signals import (
    after_setup_logger,
    after_setup_task_logger,
    worker_process_init,
)


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

from app.config import (
    CELERY_BROKER_VISIBILITY_TIMEOUT,
    CELERY_TASK_SOFT_TIME_LIMIT,
    CELERY_TASK_TIME_LIMIT,
    REDIS_URL,
    SCAN_POLL_INTERVAL_SECONDS,
)

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
        "app.tasks.generate_home_briefing",
        "app.tasks.maintenance",
    ],
)

MAINTENANCE_QUEUE = "maintenance"
# Short, DB-only housekeeping that must keep ticking while LLM/OCR work is
# backed up. Tasks that call a model (claim-embedding retry), do real ingest
# work (Gmail sync, scan-folder tick) or take a model_gate stay on their own
# queues.
MAINTENANCE_TASKS = (
    "app.tasks.maintenance.recover_pipeline_task",
    "app.tasks.maintenance.prune_ai_debug_logs_task",
    "app.tasks.thread_open_scan.thread_open_scan_task",
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
    "recover-pipeline": {
        "task": "app.tasks.maintenance.recover_pipeline_task",
        "schedule": 300.0,  # every 5 minutes — orphaned RUNNING stages reset within 5 min max
    },
    "retry-failed-claim-embeddings": {
        "task": "app.tasks.maintenance.retry_failed_claim_embeddings_task",
        "schedule": 1800.0,  # every 30 minutes — slow enough to spread load,
        # fast enough that recovery from a 30-min embedding-backend outage
        # completes within ~1 hour.
    },
}

celery_app.conf.update(
    task_serializer="json",
    accept_content=["json"],
    result_serializer="json",
    timezone="UTC",
    enable_utc=True,
    task_track_started=True,
    task_time_limit=CELERY_TASK_TIME_LIMIT,
    # Raises SoftTimeLimitExceeded inside the task so it gets a chance to
    # mark its own pipeline stage failed and return cleanly, rather than
    # running uncontrolled until the hard kill below (which gives the task
    # no chance to clean up at all — acks_late + reject_on_worker_lost then
    # redelivers it to another worker, but the DB-visible stage stays stuck
    # at RUNNING until the 30-min orphan sweep notices).
    task_soft_time_limit=CELERY_TASK_SOFT_TIME_LIMIT,
    worker_prefetch_multiplier=1,
    task_acks_late=True,
    task_reject_on_worker_lost=True,
    # Redis visibility_timeout must exceed task_time_limit — see
    # app/config.py's assertion for why. Without this, kombu's Redis
    # transport defaults to a 3600s visibility_timeout regardless of
    # task_time_limit, so the two silently drifted out of the safe
    # relationship if either was ever tuned independently.
    broker_transport_options={
        "visibility_timeout": CELERY_BROKER_VISIBILITY_TIMEOUT,
    },
    task_always_eager=os.getenv("CELERY_TASK_ALWAYS_EAGER", "false").lower() == "true",
    # Propagate exceptions from eagerly-executed tasks so cascade failures are
    # visible in logs rather than silently captured in EagerResult. Has no effect
    # when task_always_eager is False.
    task_eager_propagates=os.getenv("CELERY_TASK_ALWAYS_EAGER", "false").lower()
    == "true",
    # Three-queue split: heavy Docling/Tesseract OCR is pinned to the `ingest`
    # queue (concurrency UI-controlled, default 4 — see get_ocr_concurrency /
    # app/services/ocr_slots.py for the matching per-page semaphore),
    # everything else (LLM calls, embeddings, light I/O) lands on `ai`
    # (concurrency UI-controlled; the Docker entrypoint boots with the stored
    # value, default 2 to match LMStudio's two-slot capacity, while `make run`
    # starts 3 — see get_worker_concurrency). The periodic recovery sweep and
    # other short DB-only housekeeping run on `maintenance`, on a worker of
    # their own: the sweep is what catches a worker stuck on a long model_gate
    # wait, so it must never queue behind one (#144).
    task_default_queue="ai",
    task_routes={
        "app.tasks.document_processing.process_document_task": {"queue": "ingest"},
        **{name: {"queue": MAINTENANCE_QUEUE} for name in MAINTENANCE_TASKS},
    },
)


@worker_process_init.connect
def _dispose_inherited_db_connections(**_kwargs) -> None:
    """Give every prefork child its own DB connections.

    The parent imports app.config (and may touch the engine at boot), so its
    pooled psycopg sockets are inherited across fork(). Two children talking
    over the same socket corrupt each other's protocol state — seen as
    "can't change 'autocommit' now: INTRANS" and "server closed the connection
    unexpectedly" a second apart in different children. ``close=False`` drops
    the pool without closing sockets the parent still owns.
    """
    from app.config import engine

    engine.dispose(close=False)
