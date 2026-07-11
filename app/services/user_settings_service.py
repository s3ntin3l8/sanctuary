from __future__ import annotations

import logging
from datetime import datetime, timedelta

from sqlalchemy.exc import OperationalError

from app.core.timezone import naive_utc_now
from app.models.database import AppSettings, UserSettings
from app.models.enums import AuditEventType
from app.services import audit_service
from app.services.app_settings_service import _get_or_create as _get_or_create_app
from app.services.pipeline_status import is_db_locked

logger = logging.getLogger(__name__)

# A reindex or dedup job stuck in "running" past this threshold is assumed to
# have orphaned (worker crash, killed dev process). The hourly maintenance
# task flips it to "failed" so the user can retry. 60 min is well above any
# realistic AI-bound run on the local Ollama corpus.
STALE_JOB_THRESHOLD_SECONDS = 3600


# ===========================================================================
# Per-user settings (theme, dashboard cards, last-viewed, active proceeding).
# These require a user_id — callers pass current_user.id. Background workers
# never touch per-user settings; only the global ones (further below).
# ===========================================================================


def _get_or_create_user(db, user_id: int) -> UserSettings:
    settings = db.query(UserSettings).filter(UserSettings.user_id == user_id).first()
    if not settings:
        settings = UserSettings(user_id=user_id, settings_json={})
        db.add(settings)
        db.flush()
    return settings


def _user_settings(db, user_id: int) -> UserSettings | None:
    return db.query(UserSettings).filter(UserSettings.user_id == user_id).first()


def get_last_viewed(case_id: str, db, user_id: int) -> datetime | None:
    """Return the datetime the user last viewed this case, or None."""
    settings = _user_settings(db, user_id)
    if not settings or not settings.settings_json:
        return None
    last_viewed = settings.settings_json.get("last_viewed_cases", {})
    raw = last_viewed.get(case_id)
    if raw is None:
        return None
    return datetime.fromisoformat(raw)


def mark_viewed(case_id: str, db, user_id: int, *, now: datetime | None = None) -> None:
    """Record that the user just viewed this case (best-effort)."""
    if now is None:
        now = naive_utc_now()
    try:
        settings = _get_or_create_user(db, user_id)
        current = dict(settings.settings_json or {})
        last_viewed = dict(current.get("last_viewed_cases", {}))
        last_viewed[case_id] = now.isoformat()
        current["last_viewed_cases"] = last_viewed
        settings.settings_json = current
        db.flush()
    except OperationalError as exc:
        if is_db_locked(exc):
            logger.warning(
                "mark_viewed(%s): db locked, skipping last-viewed update", case_id
            )
            db.rollback()
            return
        raise


def get_active_proceeding(case_id: str, db, user_id: int) -> int | None:
    settings = _user_settings(db, user_id)
    if not settings or not settings.settings_json:
        return None
    value = settings.settings_json.get("active_proceeding", {}).get(case_id)
    return int(value) if value is not None else None


def set_active_proceeding(
    case_id: str, proceeding_id: int | None, db, user_id: int
) -> None:
    settings = _get_or_create_user(db, user_id)
    data = dict(settings.settings_json or {})
    active = dict(data.get("active_proceeding", {}))
    active[case_id] = proceeding_id
    data["active_proceeding"] = active
    settings.settings_json = data
    db.flush()


def mark_home_visit(db, user_id: int, *, now: datetime | None = None) -> None:
    """Record that the user just visited the home page (best-effort)."""
    if now is None:
        now = naive_utc_now()
    try:
        settings = _get_or_create_user(db, user_id)
        current = dict(settings.settings_json or {})
        current["last_home_visit"] = now.isoformat()
        settings.settings_json = current
        db.flush()
    except OperationalError as exc:
        if is_db_locked(exc):
            logger.warning("mark_home_visit: db locked, skipping update")
            db.rollback()
            return
        raise


def get_last_home_visit(db, user_id: int) -> datetime | None:
    settings = _user_settings(db, user_id)
    if not settings or not settings.settings_json:
        return None
    raw = settings.settings_json.get("last_home_visit")
    if raw is None:
        return None
    try:
        return datetime.fromisoformat(raw)
    except (TypeError, ValueError):
        return None


def set_theme(theme: str, db, user_id: int) -> None:
    settings = _get_or_create_user(db, user_id)
    data = dict(settings.settings_json or {})
    data["theme"] = theme
    settings.settings_json = data
    audit_service.record(
        db,
        AuditEventType.SETTINGS_THEME_CHANGED,
        payload={"theme": theme},
        actor_user_id=user_id,
    )
    db.flush()


def set_dashboard_cards(cards: dict, db, user_id: int) -> None:
    settings = _get_or_create_user(db, user_id)
    data = dict(settings.settings_json or {})
    data["dashboard_cards"] = cards
    settings.settings_json = data
    audit_service.record(
        db, AuditEventType.SETTINGS_DASHBOARD_CARDS_CHANGED, actor_user_id=user_id
    )
    db.flush()


# --- Per-user Gmail connection (each user connects their own mailbox) ---

_GMAIL_KEYS = (
    "gmail_credentials_json",
    "gmail_allowlist",
    "gmail_label_filter",
    "gmail_connected_at",
)


def get_gmail_config(db, user_id: int) -> dict:
    """Return this user's Gmail config keys (empty values when unset)."""
    settings = _user_settings(db, user_id)
    data = (settings.settings_json or {}) if settings else {}
    return {k: data.get(k) for k in _GMAIL_KEYS}


def set_gmail_inbox_filters(
    db, user_id: int, *, allowlist: list[str], label_filter: str
) -> None:
    settings = _get_or_create_user(db, user_id)
    data = dict(settings.settings_json or {})
    data["gmail_allowlist"] = allowlist
    data["gmail_label_filter"] = label_filter
    settings.settings_json = data
    db.flush()


def set_gmail_credentials(
    db, user_id: int, *, credentials_json: str, connected_at: str
) -> None:
    settings = _get_or_create_user(db, user_id)
    data = dict(settings.settings_json or {})
    data["gmail_credentials_json"] = credentials_json
    data["gmail_connected_at"] = connected_at
    settings.settings_json = data
    db.flush()


def user_ids_with_gmail(db) -> list[int]:
    """User ids that have connected Gmail (for the per-user sync fan-out)."""
    rows = db.query(UserSettings.user_id, UserSettings.settings_json).all()
    out: list[int] = []
    for uid, sj in rows:
        if isinstance(sj, dict) and sj.get("gmail_credentials_json"):
            out.append(uid)
    return out


def count_new_since(case_id: str, since: datetime | None, db) -> int:
    """Count documents added to the case after `since`. Returns 0 if since is None."""
    if since is None:
        return 0
    from app.models.database import Document

    return (
        db.query(Document)
        .filter(Document.case_id == case_id, Document.ingest_date > since)
        .count()
    )


# ===========================================================================
# Global settings (no user). Stored in the AppSettings singleton so background
# workers can read/write without a request. Signatures are db-only by design —
# existing callers are unchanged.
# ===========================================================================


def get_dedup_job(case_id: str, db) -> dict | None:
    data = _get_or_create_app(db).settings_json or {}
    return data.get("dedup_jobs", {}).get(case_id)


def set_dedup_running(case_id: str, db, *, total: int = 0) -> None:
    settings = _get_or_create_app(db)
    jobs = dict((settings.settings_json or {}).get("dedup_jobs", {}))
    jobs[case_id] = {
        "status": "running",
        "total": total,
        "processed": 0,
        "started_at": naive_utc_now().isoformat(),
        "ended_at": None,
    }
    settings.settings_json = {**(settings.settings_json or {}), "dedup_jobs": jobs}
    db.flush()


def update_dedup_progress(case_id: str, db, *, processed: int) -> None:
    """Update processed-count on the in-flight dedup job (best-effort)."""
    try:
        settings = _get_or_create_app(db)
        jobs = dict((settings.settings_json or {}).get("dedup_jobs", {}))
        job = dict(jobs.get(case_id) or {})
        if job.get("status") != "running":
            return
        job["processed"] = processed
        jobs[case_id] = job
        settings.settings_json = {**(settings.settings_json or {}), "dedup_jobs": jobs}
        db.flush()
    except OperationalError as exc:
        if is_db_locked(exc):
            logger.debug(
                "update_dedup_progress: db locked for case %s; skipping", case_id
            )
            return
        raise


def set_dedup_result(
    case_id: str, stats: dict | None, db, *, failed: bool = False
) -> None:
    settings = _get_or_create_app(db)
    jobs = dict((settings.settings_json or {}).get("dedup_jobs", {}))
    prior = jobs.get(case_id) or {}
    jobs[case_id] = {
        "status": "failed" if failed else "done",
        "stats": stats or {},
        "started_at": prior.get("started_at"),
        "ended_at": naive_utc_now().isoformat(),
    }
    settings.settings_json = {**(settings.settings_json or {}), "dedup_jobs": jobs}
    db.flush()


# --- Embedding reindex job state (singleton — at most one in flight globally) ---


def get_reindex_job(db) -> dict | None:
    row = db.query(AppSettings).first()
    if not row or not row.settings_json:
        return None
    return row.settings_json.get("reindex_job")


def set_reindex_running(db, *, total: int, embed_dim: int) -> None:
    settings = _get_or_create_app(db)
    settings.settings_json = {
        **(settings.settings_json or {}),
        "reindex_job": {
            "status": "running",
            "total": total,
            "reindexed": 0,
            "failed": 0,
            "started_at": naive_utc_now().isoformat(),
            "ended_at": None,
            "embed_dim": embed_dim,
            "error": None,
        },
    }
    db.flush()


def update_reindex_progress(db, *, reindexed: int, failed: int) -> None:
    """Update progress counters on the in-flight reindex job. Best-effort."""
    try:
        settings = _get_or_create_app(db)
        job = dict((settings.settings_json or {}).get("reindex_job") or {})
        if job.get("status") != "running":
            return
        job["reindexed"] = reindexed
        job["failed"] = failed
        settings.settings_json = {**(settings.settings_json or {}), "reindex_job": job}
        db.flush()
    except OperationalError as exc:
        if is_db_locked(exc):
            logger.debug("update_reindex_progress: db locked, skipping update")
            return
        raise


def set_reindex_done(db) -> None:
    settings = _get_or_create_app(db)
    job = dict((settings.settings_json or {}).get("reindex_job") or {})
    job["status"] = "done"
    job["ended_at"] = naive_utc_now().isoformat()
    settings.settings_json = {**(settings.settings_json or {}), "reindex_job": job}
    db.flush()


def set_reindex_failed(db, error: str) -> None:
    settings = _get_or_create_app(db)
    job = dict((settings.settings_json or {}).get("reindex_job") or {})
    job["status"] = "failed"
    job["ended_at"] = naive_utc_now().isoformat()
    job["error"] = error[:500]
    settings.settings_json = {**(settings.settings_json or {}), "reindex_job": job}
    db.flush()


# --- Stale-job recovery — called from the hourly maintenance task ---


def _is_stale(started_at_iso: str | None) -> bool:
    if not started_at_iso:
        return False
    try:
        started = datetime.fromisoformat(started_at_iso)
    except (TypeError, ValueError):
        return False
    return naive_utc_now() - started > timedelta(seconds=STALE_JOB_THRESHOLD_SECONDS)


def recover_stale_reindex_job(db) -> bool:
    """Flip a stuck reindex_job from 'running' to 'failed'."""
    job = get_reindex_job(db)
    if not job or job.get("status") != "running":
        return False
    if not _is_stale(job.get("started_at")):
        return False
    set_reindex_failed(db, "stale: no progress for >60min")
    return True


def recover_stale_dedup_jobs(db) -> list[str]:
    """Flip every stuck dedup job (per case) from 'running' to 'failed'."""
    row = db.query(AppSettings).first()
    if not row or not row.settings_json:
        return []
    jobs = row.settings_json.get("dedup_jobs", {}) or {}
    flipped: list[str] = []
    for case_id, job in jobs.items():
        if not isinstance(job, dict) or job.get("status") != "running":
            continue
        if not _is_stale(job.get("started_at")):
            continue
        set_dedup_result(
            case_id, {"error": "stale: no progress for >60min"}, db, failed=True
        )
        flipped.append(case_id)
    return flipped


def get_party_identity(db) -> dict:
    """Return global party identity: {own_self, own_parties}."""
    data = _get_or_create_app(db).settings_json or {}
    stored = data.get("party_identity", {})
    return {
        "own_self": stored.get("own_self", ""),
        "own_parties": stored.get("own_parties", []),
    }


def set_party_identity(identity: dict, db) -> None:
    """Persist global party identity (own_self, own_parties; opposing is per-case)."""
    settings = _get_or_create_app(db)
    data = dict(settings.settings_json or {})
    data["party_identity"] = {
        "own_self": (identity.get("own_self") or "").strip(),
        "own_parties": [
            p.strip()
            for p in (identity.get("own_parties") or [])
            if p and str(p).strip()
        ],
    }
    settings.settings_json = data
    audit_service.record(db, AuditEventType.SETTINGS_PARTIES_CHANGED)
    db.flush()


def get_ai_debug_redact(db) -> bool:
    """Return True if AI debug log message bodies should be redacted."""
    data = _get_or_create_app(db).settings_json or {}
    return bool(data.get("ai", {}).get("ai_debug_redact", False))


VALID_EXTRACTION_ENGINES = ("chandra", "docling")
DEFAULT_EXTRACTION_ENGINE = "chandra"


def get_extraction_engine(db) -> str:
    """Return the configured PDF extraction engine."""
    data = _get_or_create_app(db).settings_json or {}
    engine = data.get("ingestion", {}).get("extraction_engine")
    if engine in VALID_EXTRACTION_ENGINES:
        return engine
    return DEFAULT_EXTRACTION_ENGINE


def set_extraction_engine(db, engine: str) -> None:
    """Persist the PDF extraction engine selection."""
    if engine not in VALID_EXTRACTION_ENGINES:
        raise ValueError(
            f"Unknown engine {engine!r}; expected one of {VALID_EXTRACTION_ENGINES}"
        )
    settings = _get_or_create_app(db)
    data = dict(settings.settings_json or {})
    ingestion = dict(data.get("ingestion", {}))
    ingestion["extraction_engine"] = engine
    data["ingestion"] = ingestion
    settings.settings_json = data
    audit_service.record(
        db,
        AuditEventType.SETTINGS_INGESTION_CHANGED,
        payload={"extraction_engine": engine},
    )
    db.commit()


AI_CONCURRENCY_MIN = 1
AI_CONCURRENCY_MAX = 16
DEFAULT_AI_CONCURRENCY = 2


def get_worker_concurrency(db) -> int:
    """Return the configured `ai` Celery worker concurrency (1–16, default 2).

    Read at worker boot (via app.cli.worker_concurrency) so the DB is the single
    source of truth, and shown in the Settings UI. The `ingest` queue has its
    own independent knob — see get_ocr_concurrency().
    """
    data = _get_or_create_app(db).settings_json or {}
    val = data.get("workers", {}).get("ai_concurrency")
    if isinstance(val, int) and AI_CONCURRENCY_MIN <= val <= AI_CONCURRENCY_MAX:
        return val
    return DEFAULT_AI_CONCURRENCY


def set_worker_concurrency(db, concurrency: int) -> None:
    """Persist the `ai` worker concurrency. Raises ValueError if out of bounds."""
    if not isinstance(concurrency, int) or not (
        AI_CONCURRENCY_MIN <= concurrency <= AI_CONCURRENCY_MAX
    ):
        raise ValueError(
            f"Concurrency must be an integer {AI_CONCURRENCY_MIN}–{AI_CONCURRENCY_MAX}"
        )
    settings = _get_or_create_app(db)
    data = dict(settings.settings_json or {})
    workers = dict(data.get("workers", {}))
    workers["ai_concurrency"] = concurrency
    data["workers"] = workers
    settings.settings_json = data
    audit_service.record(
        db,
        AuditEventType.SETTINGS_WORKERS_CHANGED,
        payload={"ai_concurrency": concurrency},
    )
    db.commit()


OCR_CONCURRENCY_MIN = 1
OCR_CONCURRENCY_MAX = 16
DEFAULT_OCR_CONCURRENCY = 4


def get_ocr_concurrency(db) -> int:
    """Return the configured global OCR-model slot count (1-16, default 4).

    This caps two things together: the `ingest` Celery worker's prefork pool
    (how many documents are extracted at once) and the per-page
    ``ocr_slots.ocr_slot()`` semaphore (the total concurrent OCR-model HTTP
    calls across all of those documents) — see app/services/ocr_slots.py.
    Read at worker boot (via app.cli.ocr_concurrency) so the DB is the single
    source of truth, and shown in the Settings UI.
    """
    data = _get_or_create_app(db).settings_json or {}
    val = data.get("workers", {}).get("ocr_concurrency")
    if isinstance(val, int) and OCR_CONCURRENCY_MIN <= val <= OCR_CONCURRENCY_MAX:
        return val
    return DEFAULT_OCR_CONCURRENCY


def set_ocr_concurrency(db, concurrency: int) -> None:
    """Persist the OCR-slot concurrency. Raises ValueError if out of bounds."""
    if not isinstance(concurrency, int) or not (
        OCR_CONCURRENCY_MIN <= concurrency <= OCR_CONCURRENCY_MAX
    ):
        raise ValueError(
            f"Concurrency must be an integer {OCR_CONCURRENCY_MIN}–{OCR_CONCURRENCY_MAX}"
        )
    settings = _get_or_create_app(db)
    data = dict(settings.settings_json or {})
    workers = dict(data.get("workers", {}))
    workers["ocr_concurrency"] = concurrency
    data["workers"] = workers
    settings.settings_json = data
    audit_service.record(
        db,
        AuditEventType.SETTINGS_WORKERS_CHANGED,
        payload={"ocr_concurrency": concurrency},
    )
    db.commit()


def set_ai_debug_redact(db, value: bool) -> None:
    """Persist the AI debug log redaction toggle and emit an audit event."""
    settings = _get_or_create_app(db)
    data = dict(settings.settings_json or {})
    ai = dict(data.get("ai", {}))
    ai["ai_debug_redact"] = value
    data["ai"] = ai
    settings.settings_json = data
    audit_service.record(
        db, AuditEventType.AI_DEBUG_REDACT_TOGGLED, payload={"enabled": value}
    )
    db.commit()
