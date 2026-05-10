import fcntl
import json
import logging
import os
from datetime import UTC, datetime, timedelta
from pathlib import Path

from app.tasks.celery_app import celery_app

logger = logging.getLogger(__name__)

_MAX_AGE_DAYS = 30
_SEPARATOR = "═" * 64


def _prune_log_file(log_file: Path, cutoff: datetime) -> tuple[int, int]:
    """Prune blocks older than cutoff from a single .log file.

    Returns (blocks_kept, blocks_pruned). Removes the file if all blocks pruned.
    Blocks without a parsable ts= header (legacy format) are always pruned.
    """
    with open(log_file, "r+", encoding="utf-8", errors="replace") as f:
        fcntl.flock(f.fileno(), fcntl.LOCK_EX)
        try:
            content = f.read()
            raw_blocks = content.split(_SEPARATOR)

            kept = []
            pruned = 0
            for block in raw_blocks:
                block = block.strip()
                if not block:
                    continue
                ts = _parse_block_ts(block)
                if ts is not None and ts >= cutoff:
                    kept.append(block)
                else:
                    pruned += 1

            if not kept:
                f.close()
                log_file.unlink(missing_ok=True)
                return 0, pruned

            new_content = f"\n{_SEPARATOR}\n".join(kept) + "\n"
            tmp = log_file.with_suffix(".tmp")
            tmp.write_text(new_content, encoding="utf-8")
            os.replace(tmp, log_file)
            return len(kept), pruned
        finally:
            try:
                fcntl.flock(f.fileno(), fcntl.LOCK_UN)
            except Exception:
                pass


def _parse_block_ts(block: str) -> datetime | None:
    """Extract the ts= timestamp from the second line of a block header."""
    for line in block.splitlines()[:3]:
        if "ts=" in line:
            for part in line.split("|"):
                part = part.strip()
                if part.startswith("ts="):
                    raw = part[3:].strip()
                    try:
                        return datetime.fromisoformat(raw.rstrip("Z")).replace(
                            tzinfo=UTC
                        )
                    except ValueError:
                        return None
    return None


def _prune_jsonl(jsonl_file: Path, cutoff: datetime) -> tuple[int, int]:
    """Prune entries older than cutoff from runs.jsonl.

    Returns (kept, pruned). Removes the file if all entries pruned.
    """
    with open(jsonl_file, "r+", encoding="utf-8", errors="replace") as f:
        fcntl.flock(f.fileno(), fcntl.LOCK_EX)
        try:
            lines = f.readlines()
            kept = []
            pruned = 0
            for line in lines:
                line = line.strip()
                if not line:
                    continue
                try:
                    entry = json.loads(line)
                    ts_raw = entry.get("ts", "")
                    ts = datetime.fromisoformat(ts_raw.rstrip("Z")).replace(tzinfo=UTC)
                    if ts >= cutoff:
                        kept.append(line)
                    else:
                        pruned += 1
                except Exception:
                    pruned += 1

            if not kept:
                f.close()
                jsonl_file.unlink(missing_ok=True)
                return 0, pruned

            tmp = jsonl_file.with_suffix(".tmp")
            tmp.write_text("\n".join(kept) + "\n", encoding="utf-8")
            os.replace(tmp, jsonl_file)
            return len(kept), pruned
        finally:
            try:
                fcntl.flock(f.fileno(), fcntl.LOCK_UN)
            except Exception:
                pass


@celery_app.task(name="app.tasks.maintenance.prune_ai_debug_logs_task")
def prune_ai_debug_logs_task():
    """Prune ai_debug log blocks and index entries older than 30 days."""
    from app.config import DATA_DIR

    debug_dir = DATA_DIR / "ai_debug"
    if not debug_dir.exists():
        return {"status": "skipped", "reason": "ai_debug directory does not exist"}

    cutoff = datetime.now(UTC) - timedelta(days=_MAX_AGE_DAYS)

    blocks_kept = 0
    blocks_pruned = 0
    files_removed = 0
    errors = 0

    for entry in debug_dir.iterdir():
        if not entry.is_file():
            continue
        try:
            if entry.name == "runs.jsonl":
                k, p = _prune_jsonl(entry, cutoff)
                blocks_kept += k
                blocks_pruned += p
                if not entry.exists():
                    files_removed += 1
            elif entry.suffix == ".log":
                k, p = _prune_log_file(entry, cutoff)
                blocks_kept += k
                blocks_pruned += p
                if not entry.exists():
                    files_removed += 1
        except OSError as exc:
            logger.warning("Could not prune ai_debug file %s: %s", entry, exc)
            errors += 1

    logger.info(
        "prune_ai_debug_logs: kept=%d pruned=%d files_removed=%d errors=%d cutoff=%s",
        blocks_kept,
        blocks_pruned,
        files_removed,
        errors,
        cutoff.date().isoformat(),
    )
    return {
        "status": "success",
        "blocks_kept": blocks_kept,
        "blocks_pruned": blocks_pruned,
        "files_removed": files_removed,
        "errors": errors,
    }


@celery_app.task(name="app.tasks.maintenance.recover_pipeline_task")
def recover_pipeline_task():
    """Run all pipeline recovery heuristics (orphaned stages, stuck dispatches, stuck batches)."""
    from app.config import SessionLocal
    from app.services.pipeline_status import (
        recover_orphaned_running_stages,
        recover_stuck_batches,
        recover_stuck_pending_dispatches,
    )

    db = SessionLocal()
    try:
        orphaned = recover_orphaned_running_stages(db)
        dispatches = recover_stuck_pending_dispatches(db)
        batches = recover_stuck_batches(db)

        result = {
            "status": "success",
            "orphaned_docs": orphaned.get("docs_reset", 0),
            "orphaned_stages": orphaned.get("stages_reset", 0),
            "stuck_dispatches": dispatches.get("docs_redispatched", 0),
            "stuck_batches": batches.get("batches_recovered", 0),
        }
        logger.info("recover_pipeline: %s", result)
        return result
    except Exception as e:
        logger.error("recover_pipeline failed: %s", e, exc_info=True)
        return {"status": "error", "error": str(e)}
    finally:
        db.close()
