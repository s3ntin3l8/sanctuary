from __future__ import annotations

import logging
from datetime import datetime, timedelta

from sqlalchemy.exc import OperationalError

from app.core.timezone import naive_utc_now
from app.models.database import UserSettings
from app.models.enums import AuditEventType
from app.services import audit_service
from app.services.pipeline_status import is_db_locked

logger = logging.getLogger(__name__)

# A reindex or dedup job stuck in "running" past this threshold is assumed to
# have orphaned (worker crash, killed dev process). The hourly maintenance
# task flips it to "failed" so the user can retry. 60 min is well above any
# realistic AI-bound run on the local Ollama corpus.
STALE_JOB_THRESHOLD_SECONDS = 3600


def get_last_viewed(case_id: str, db) -> datetime | None:
    """Return the datetime the current user last viewed this case, or None."""
    settings = db.query(UserSettings).first()
    if not settings or not settings.settings_json:
        return None
    last_viewed = settings.settings_json.get("last_viewed_cases", {})
    raw = last_viewed.get(case_id)
    if raw is None:
        return None
    return datetime.fromisoformat(raw)


def mark_viewed(case_id: str, db, *, now: datetime | None = None) -> None:
    """Record that the user just viewed this case.

    Best-effort: this is a UX nicety (last-viewed tracking). If a Celery
    writer (claim extraction, dedup judge) is holding the SQLite write
    lock past busy_timeout, swallow `database is locked` rather than 500
    the case-page render. The user can always view the case again.
    """
    if now is None:
        now = naive_utc_now()  # naive UTC, consistent with Document.ingest_date
    try:
        settings = db.query(UserSettings).first()
        if not settings:
            return
        # Reassign entire dict to trigger SQLAlchemy JSON mutation detection
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


def _get_or_create(db) -> UserSettings:
    settings = db.query(UserSettings).first()
    if not settings:
        settings = UserSettings(settings_json={})
        db.add(settings)
        db.flush()
    return settings


def get_active_proceeding(case_id: str, db) -> int | None:
    settings = db.query(UserSettings).first()
    if not settings:
        return None
    value = settings.settings_json.get("active_proceeding", {}).get(case_id)
    return int(value) if value is not None else None


def set_active_proceeding(case_id: str, proceeding_id: int, db) -> None:
    settings = _get_or_create(db)
    data = dict(settings.settings_json)
    active = dict(data.get("active_proceeding", {}))
    active[case_id] = proceeding_id
    data["active_proceeding"] = active
    settings.settings_json = data
    db.flush()


def get_dedup_job(case_id: str, db) -> dict | None:
    settings = db.query(UserSettings).first()
    if not settings:
        return None
    return settings.settings_json.get("dedup_jobs", {}).get(case_id)


def set_dedup_running(case_id: str, db, *, total: int = 0) -> None:
    settings = _get_or_create(db)
    jobs = dict(settings.settings_json.get("dedup_jobs", {}))
    jobs[case_id] = {
        "status": "running",
        "total": total,
        "processed": 0,
        "started_at": naive_utc_now().isoformat(),
        "ended_at": None,
    }
    settings.settings_json = {**settings.settings_json, "dedup_jobs": jobs}
    db.flush()


def update_dedup_progress(case_id: str, db, *, processed: int) -> None:
    """Update processed-count on the in-flight dedup job for this case.

    Best-effort: swallows OperationalError on db-lock contention so a busy
    SQLite writer doesn't kill the background task.
    """
    try:
        settings = _get_or_create(db)
        jobs = dict(settings.settings_json.get("dedup_jobs", {}))
        job = dict(jobs.get(case_id) or {})
        if job.get("status") != "running":
            return
        job["processed"] = processed
        jobs[case_id] = job
        settings.settings_json = {**settings.settings_json, "dedup_jobs": jobs}
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
    settings = _get_or_create(db)
    jobs = dict(settings.settings_json.get("dedup_jobs", {}))
    prior = jobs.get(case_id) or {}
    jobs[case_id] = {
        "status": "failed" if failed else "done",
        "stats": stats or {},
        # Preserve started_at when the prior job carried one; ended_at is
        # always set fresh on transition.
        "started_at": prior.get("started_at"),
        "ended_at": naive_utc_now().isoformat(),
    }
    settings.settings_json = {**settings.settings_json, "dedup_jobs": jobs}
    db.flush()


# ---------------------------------------------------------------------------
# Embedding reindex job state (singleton — at most one in flight globally)
# ---------------------------------------------------------------------------


def get_reindex_job(db) -> dict | None:
    """Return the current reindex job state, or None if none has ever run."""
    settings = db.query(UserSettings).first()
    if not settings or not settings.settings_json:
        return None
    return settings.settings_json.get("reindex_job")


def set_reindex_running(db, *, total: int, embed_dim: int) -> None:
    settings = _get_or_create(db)
    settings.settings_json = {
        **settings.settings_json,
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
        settings = _get_or_create(db)
        job = dict(settings.settings_json.get("reindex_job") or {})
        if job.get("status") != "running":
            return
        job["reindexed"] = reindexed
        job["failed"] = failed
        settings.settings_json = {**settings.settings_json, "reindex_job": job}
        db.flush()
    except OperationalError as exc:
        if is_db_locked(exc):
            logger.debug("update_reindex_progress: db locked, skipping update")
            return
        raise


def set_reindex_done(db) -> None:
    settings = _get_or_create(db)
    job = dict(settings.settings_json.get("reindex_job") or {})
    job["status"] = "done"
    job["ended_at"] = naive_utc_now().isoformat()
    settings.settings_json = {**settings.settings_json, "reindex_job": job}
    db.flush()


def set_reindex_failed(db, error: str) -> None:
    settings = _get_or_create(db)
    job = dict(settings.settings_json.get("reindex_job") or {})
    job["status"] = "failed"
    job["ended_at"] = naive_utc_now().isoformat()
    job["error"] = error[:500]
    settings.settings_json = {**settings.settings_json, "reindex_job": job}
    db.flush()


# ---------------------------------------------------------------------------
# Stale-job recovery — called from the hourly maintenance task
# ---------------------------------------------------------------------------


def _is_stale(started_at_iso: str | None) -> bool:
    """True when started_at + STALE_JOB_THRESHOLD_SECONDS is in the past.
    Returns False when started_at is missing/unparseable (defensive: pre-existing
    jobs written before this field existed shouldn't be auto-failed)."""
    if not started_at_iso:
        return False
    try:
        started = datetime.fromisoformat(started_at_iso)
    except (TypeError, ValueError):
        return False
    return naive_utc_now() - started > timedelta(seconds=STALE_JOB_THRESHOLD_SECONDS)


def recover_stale_reindex_job(db) -> bool:
    """Flip a stuck reindex_job from 'running' to 'failed'.

    Returns True when the job was flipped, False otherwise. No-op when the
    job is missing, not running, or fresh.
    """
    job = get_reindex_job(db)
    if not job or job.get("status") != "running":
        return False
    if not _is_stale(job.get("started_at")):
        return False
    set_reindex_failed(db, "stale: no progress for >60min")
    return True


def recover_stale_dedup_jobs(db) -> list[str]:
    """Flip every stuck dedup job (per case) from 'running' to 'failed'.

    Returns the list of case_ids that were flipped, for logging.
    """
    settings = db.query(UserSettings).first()
    if not settings or not settings.settings_json:
        return []
    jobs = settings.settings_json.get("dedup_jobs", {}) or {}
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


def mark_home_visit(db, *, now: datetime | None = None) -> None:
    """Record that the user just visited the home page. Best-effort —
    swallows SQLite write-lock contention rather than 500 the home page."""
    if now is None:
        now = naive_utc_now()
    try:
        settings = _get_or_create(db)
        # Reassign entire dict to trigger SQLAlchemy JSON mutation detection
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


def get_party_identity(db) -> dict:
    """Return global party identity: {own_self, own_parties}.

    opposing_parties is per-case (on Case.opposing_parties) — not returned here.
    """
    defaults: dict = {"own_self": "", "own_parties": []}
    settings = db.query(UserSettings).first()
    if not settings or not settings.settings_json:
        return defaults
    stored = settings.settings_json.get("party_identity", {})
    return {
        "own_self": stored.get("own_self", ""),
        "own_parties": stored.get("own_parties", []),
    }


def set_party_identity(identity: dict, db) -> None:
    """Persist global party identity (own_self, own_parties only — opposing is per-case)."""
    settings = _get_or_create(db)
    data = dict(settings.settings_json)
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


def set_theme(theme: str, db) -> None:
    settings = _get_or_create(db)
    data = dict(settings.settings_json)
    data["theme"] = theme
    settings.settings_json = data
    audit_service.record(
        db, AuditEventType.SETTINGS_THEME_CHANGED, payload={"theme": theme}
    )
    db.flush()


def set_dashboard_cards(cards: dict, db) -> None:
    settings = _get_or_create(db)
    data = dict(settings.settings_json)
    data["dashboard_cards"] = cards
    settings.settings_json = data
    audit_service.record(db, AuditEventType.SETTINGS_DASHBOARD_CARDS_CHANGED)
    db.flush()


def get_ai_debug_redact(db) -> bool:
    """Return True if AI debug log message bodies should be redacted."""
    settings = db.query(UserSettings).first()
    if settings and isinstance(settings.settings_json, dict):
        return bool(settings.settings_json.get("ai", {}).get("ai_debug_redact", False))
    return False


# ---------------------------------------------------------------------------
# Document ingestion engine (Chandra OCR vs Docling+Tesseract for PDFs)
# ---------------------------------------------------------------------------

VALID_EXTRACTION_ENGINES = ("chandra", "docling")
DEFAULT_EXTRACTION_ENGINE = "chandra"


def get_extraction_engine(db) -> str:
    """Return the configured PDF extraction engine.

    Defaults to "chandra" so new installs get the better-quality extractor
    out of the box. Falls back to "docling" only if explicitly set.
    """
    settings = db.query(UserSettings).first()
    if settings and isinstance(settings.settings_json, dict):
        engine = settings.settings_json.get("ingestion", {}).get("extraction_engine")
        if engine in VALID_EXTRACTION_ENGINES:
            return engine
    return DEFAULT_EXTRACTION_ENGINE


def set_extraction_engine(db, engine: str) -> None:
    """Persist the PDF extraction engine selection."""
    if engine not in VALID_EXTRACTION_ENGINES:
        raise ValueError(
            f"Unknown engine {engine!r}; expected one of {VALID_EXTRACTION_ENGINES}"
        )
    settings = _get_or_create(db)
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


def set_ai_debug_redact(db, value: bool) -> None:
    """Persist the AI debug log redaction toggle and emit an audit event."""
    settings = _get_or_create(db)
    data = dict(settings.settings_json or {})
    ai = dict(data.get("ai", {}))
    ai["ai_debug_redact"] = value
    data["ai"] = ai
    settings.settings_json = data
    audit_service.record(
        db, AuditEventType.AI_DEBUG_REDACT_TOGGLED, payload={"enabled": value}
    )
    db.commit()


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
