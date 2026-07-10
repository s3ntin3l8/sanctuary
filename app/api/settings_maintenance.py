"""Data & Maintenance settings endpoints."""

import logging
import shutil
from pathlib import Path

from fastapi import APIRouter, Depends, Request
from fastapi.responses import HTMLResponse
from sqlalchemy import text
from sqlalchemy.orm import Session

from app import config as cfg
from app.core.cache import cache
from app.core.rate_limit import limiter
from app.dependencies import get_db
from app.models.database import Base
from app.models.enums import AuditEventType
from app.services import audit_service
from app.services.case_service import seed_triage_case

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/api/settings/maintenance", tags=["settings"])

# Tables preserved across a workspace clear: the account, its per-user prefs,
# global app/AI config (API keys, connected accounts, bootstrap_admin pin), and
# the audit trail. Everything else is workspace/domain data and is wiped.
_PRESERVED_TABLES = ("users", "user_settings", "app_settings", "audit_logs")


@router.post("/reset-enrichment", response_class=HTMLResponse)
@limiter.limit("5/minute")
def reset_ai_enrichment(request: Request, db: Session = Depends(get_db)):
    vectors_cleared = db.execute(text("DELETE FROM document_vectors")).rowcount

    result = db.execute(
        text(
            "UPDATE documents SET "
            "ai_summary = NULL, ai_summary_created_at = NULL, "
            "significance_tier = NULL, key_passages = NULL "
            "WHERE 1=1"
        )
    )
    docs_reset = result.rowcount
    audit_service.record(db, AuditEventType.MAINTENANCE_RESET_AI_ENRICHMENT)
    db.commit()

    return HTMLResponse(
        f'<span class="text-xs" style="color:var(--color-primary)">'
        f"Reset {docs_reset} document{'' if docs_reset == 1 else 's'}; {vectors_cleared} embedding{'' if vectors_cleared == 1 else 's'} cleared."
        f"</span>"
    )


@router.post("/clear-all-data", response_class=HTMLResponse)
@limiter.limit("5/minute")
def clear_all_data(request: Request, db: Session = Depends(get_db)):
    # Purge queued Celery tasks so in-flight jobs don't repopulate rows.
    try:
        from app.tasks.celery_app import celery_app  # noqa: PLC0415

        celery_app.control.purge()
    except Exception as exc:
        logger.warning("Could not purge Celery queue: %s", exc)

    # Wipe all domain tables; skip _PRESERVED_TABLES and the sqlite-vec virtual
    # table. NOTE: "users" must stay in _PRESERVED_TABLES — user_settings has an
    # ondelete="CASCADE" FK to users, and PRAGMA foreign_keys=ON is set on every
    # connection (app/config.py), so deleting users would cascade-delete
    # user_settings regardless of this skip list.
    db.execute(text("DELETE FROM document_vectors"))
    rows_deleted = 0
    for table in reversed(Base.metadata.sorted_tables):
        if table.name in _PRESERVED_TABLES:
            continue
        rows_deleted += db.execute(table.delete()).rowcount
    audit_service.record(db, AuditEventType.MAINTENANCE_CLEAR_ALL_DATA)
    db.commit()

    # Restore the _TRIAGE singleton — many ingest paths require this FK target.
    seed_triage_case(db)
    # seed_triage_case early-returns without committing when _TRIAGE already
    # exists (it doesn't here, since cases was just wiped, but don't rely on
    # that) — ensure the session's write transaction is closed before VACUUM,
    # which cannot run while any connection holds a write lock.
    db.commit()

    # Reclaim freed pages so the on-disk file (and the "DB size" stat) actually
    # shrinks — SQLite does not shrink on DELETE without an explicit VACUUM.
    # Use db.get_bind(), not the module-level `engine` import: the test suite
    # rebinds SessionLocal to a separate test engine without touching
    # app.config.engine, so a hardcoded `engine.connect()` here would silently
    # VACUUM the wrong database (or the real dev DB) under test.
    try:
        with db.get_bind().connect() as conn:
            conn.execution_options(isolation_level="AUTOCOMMIT")
            conn.exec_driver_sql("VACUUM")
    except Exception as exc:
        logger.warning("VACUUM after clear-all-data failed: %s", exc)

    # Wipe filesystem artifacts.
    _SYSTEM_DIRS = {"_TRIAGE", "scans", "ai_debug"}
    disk_items = 0

    def _clear_dir_contents(path: Path) -> int:
        removed = 0
        if not path.exists():
            return removed
        for item in path.iterdir():
            try:
                shutil.rmtree(item) if item.is_dir() else item.unlink()
                removed += 1
            except Exception as exc:
                logger.warning("Could not remove %s: %s", item, exc)
        return removed

    # Per-case directories (anything in DATA_DIR that isn't a known system dir).
    for entry in cfg.DATA_DIR.iterdir():
        if entry.is_dir() and entry.name not in _SYSTEM_DIRS:
            try:
                shutil.rmtree(entry)
                disk_items += 1
            except Exception as exc:
                logger.warning("Could not remove %s: %s", entry, exc)

    # _TRIAGE upload artifacts (keep the dir itself).
    disk_items += _clear_dir_contents(cfg.DATA_DIR / "_TRIAGE")

    # Scan pipeline staging dirs (keep the dirs, clear contents).
    for scan_dir in (
        cfg.SCAN_INCOMING_DIR,
        cfg.SCAN_PROCESSING_DIR,
        cfg.SCAN_PROCESSED_DIR,
        cfg.SCAN_FAILED_DIR,
    ):
        disk_items += _clear_dir_contents(scan_dir)

    # AI debug logs (keep the dir, clear contents).
    disk_items += _clear_dir_contents(cfg.DATA_DIR / "ai_debug")

    cache.clear()

    s = lambda n: "" if n == 1 else "s"  # noqa: E731
    return HTMLResponse(
        f'<span class="text-xs" style="color:var(--color-primary)">'
        f"Cleared {rows_deleted} database row{s(rows_deleted)}; "
        f"{disk_items} disk artifact{s(disk_items)} removed."
        f"</span>"
    )
