"""Data & Maintenance settings endpoints."""

import logging
import shutil
from pathlib import Path

from fastapi import APIRouter, Depends
from fastapi.responses import HTMLResponse
from sqlalchemy import text
from sqlalchemy.orm import Session

from app import config as cfg
from app.core.cache import cache
from app.dependencies import get_db
from app.models.database import Base
from app.services.case_service import seed_triage_case

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/api/settings/maintenance", tags=["settings"])


@router.post("/reset-enrichment", response_class=HTMLResponse)
def reset_ai_enrichment(db: Session = Depends(get_db)):
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
    db.commit()

    return HTMLResponse(
        f'<span class="text-xs" style="color:var(--color-primary)">'
        f"Reset {docs_reset} document{'' if docs_reset == 1 else 's'}; {vectors_cleared} embedding{'' if vectors_cleared == 1 else 's'} cleared."
        f"</span>"
    )


@router.post("/clear-all-data", response_class=HTMLResponse)
def clear_all_data(db: Session = Depends(get_db)):
    # Purge queued Celery tasks so in-flight jobs don't repopulate rows.
    try:
        from app.tasks.celery_app import celery_app  # noqa: PLC0415

        celery_app.control.purge()
    except Exception as exc:
        logger.warning("Could not purge Celery queue: %s", exc)

    # Wipe all domain tables; skip user_settings and the sqlite-vec virtual table.
    db.execute(text("DELETE FROM document_vectors"))
    rows_deleted = 0
    for table in reversed(Base.metadata.sorted_tables):
        if table.name == "user_settings":
            continue
        rows_deleted += db.execute(table.delete()).rowcount
    db.commit()

    # Restore the _TRIAGE singleton — many ingest paths require this FK target.
    seed_triage_case(db)

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
