import fcntl
import json
import logging
from datetime import UTC, datetime, timedelta
from pathlib import Path

from app.core.private_files import is_current, write_private
from app.core.safe_error import describe_exception
from app.services import ai_debug_store
from app.tasks.celery_app import celery_app

logger = logging.getLogger(__name__)

# The metadata index is cheap and holds no document text; the full prompt dumps
# do, so they are kept shorter.
_INDEX_MAX_AGE_DAYS = 30
_DUMP_MAX_AGE_DAYS = 7


def _prune_log_file(log_file: Path, cutoff: datetime) -> tuple[int, int]:
    """Prune calls older than cutoff from a single encrypted .md dump.

    Returns (blocks_kept, blocks_pruned). Removes the file if all calls pruned.
    Calls without a parsable ts= header (or that no longer decrypt) are always
    pruned. Kept calls are rewritten exactly as stored (still encrypted).
    """
    with open(log_file, "r+", encoding="utf-8", errors="replace") as f:
        fcntl.flock(f.fileno(), fcntl.LOCK_EX)
        try:
            if not is_current(f.fileno(), log_file):
                return 0, 0  # replaced or removed while we waited: next run
            kept = []
            pruned = 0
            for line, block in ai_debug_store.read_blocks(f.read()):
                ts = _parse_block_ts(block.strip())
                if ts is not None and ts >= cutoff:
                    kept.append(line)
                else:
                    pruned += 1

            # Both outcomes happen under the lock; an appender that already
            # opened the old file re-checks the path once it holds the lock
            # (private_files.append_line_locked) and starts over.
            if not kept:
                log_file.unlink(missing_ok=True)
                return 0, pruned

            write_private(log_file, ("\n".join(kept) + "\n").encode("utf-8"))
            return len(kept), pruned
        finally:
            fcntl.flock(f.fileno(), fcntl.LOCK_UN)


def _parse_block_ts(block: str) -> datetime | None:
    """Extract the ts= timestamp from the second line of a block header."""
    for line in block.splitlines()[:3]:
        if "ts=" in line:
            for part in line.split("|"):
                part = part.strip()
                if part.startswith("ts="):
                    raw = part[3:].strip()
                    try:
                        dt = datetime.fromisoformat(raw)
                        return (
                            dt.astimezone(UTC) if dt.tzinfo else dt.replace(tzinfo=UTC)
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
            if not is_current(f.fileno(), jsonl_file):
                return 0, 0  # replaced or removed while we waited: next run
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
                    dt = datetime.fromisoformat(ts_raw)
                    ts = dt.astimezone(UTC) if dt.tzinfo else dt.replace(tzinfo=UTC)
                    if ts >= cutoff:
                        kept.append(line)
                    else:
                        pruned += 1
                except Exception:
                    pruned += 1

            if not kept:
                jsonl_file.unlink(missing_ok=True)
                return 0, pruned

            write_private(jsonl_file, ("\n".join(kept) + "\n").encode("utf-8"))
            return len(kept), pruned
        finally:
            fcntl.flock(f.fileno(), fcntl.LOCK_UN)


@celery_app.task(name="app.tasks.maintenance.reseal_stored_secrets_task")
def reseal_stored_secrets_task():
    """Seal stored mail and credentials with the current SECRETS_ENCRYPTION_KEY:
    encrypt leftover plaintext, move values off a retired key."""
    from app.dependencies import get_db_session
    from app.services import reseal

    db = get_db_session()
    try:
        report = reseal.reseal_all(db)
    finally:
        db.close()
    return {
        "encrypted": report.encrypted,
        "rotated": report.rotated,
        "undecryptable": report.undecryptable,
    }


@celery_app.task(name="app.tasks.maintenance.prune_ai_debug_logs_task")
def prune_ai_debug_logs_task():
    """Prune ai_debug dumps older than 7 days and index entries older than 30."""
    from app.config import DATA_DIR

    debug_dir = DATA_DIR / "ai_debug"
    if not debug_dir.exists():
        return {"status": "skipped", "reason": "ai_debug directory does not exist"}

    now = datetime.now(UTC)
    index_cutoff = now - timedelta(days=_INDEX_MAX_AGE_DAYS)
    dump_cutoff = now - timedelta(days=_DUMP_MAX_AGE_DAYS)

    blocks_kept = 0
    blocks_pruned = 0
    files_removed = 0
    errors = 0

    # Prune global index
    runs_file = debug_dir / "runs.jsonl"
    if runs_file.is_file():
        try:
            k, p = _prune_jsonl(runs_file, index_cutoff)
            blocks_kept += k
            blocks_pruned += p
            if not runs_file.exists():
                files_removed += 1
        except OSError as exc:
            logger.warning(
                "Could not prune ai_debug file %s: %s",
                runs_file,
                describe_exception(exc),
            )
            errors += 1

    # Prune the encrypted per-scope dumps
    for entry in debug_dir.rglob("*.md"):
        if not entry.is_file():
            continue
        try:
            k, p = _prune_log_file(entry, dump_cutoff)
            blocks_kept += k
            blocks_pruned += p
            if not entry.exists():
                files_removed += 1
        except OSError as exc:
            logger.warning(
                "Could not prune ai_debug file %s: %s", entry, describe_exception(exc)
            )
            errors += 1

    # Cleanup empty directories
    for subdir in debug_dir.iterdir():
        if subdir.is_dir():
            try:
                # rmdir safely raises OSError if not empty
                subdir.rmdir()
            except OSError:
                pass

    logger.info(
        "prune_ai_debug_logs: kept=%d pruned=%d files_removed=%d errors=%d dump_cutoff=%s",
        blocks_kept,
        blocks_pruned,
        files_removed,
        errors,
        dump_cutoff.date().isoformat(),
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
    """Run all pipeline + background-job recovery heuristics (orphaned stages,
    stuck dispatches, stuck batches, unclaimed metadata-phase barriers, empty
    batches, EMBEDDINGS stranded behind a failed stage, lost slicing
    preparation, stale reindex/dedup jobs)."""
    from app.config import SessionLocal
    from app.services.pipeline_status import (
        recover_embeddings_behind_failed_stage,
        recover_empty_batches,
        recover_orphaned_running_stages,
        recover_stuck_batches,
        recover_stuck_pending_dispatches,
        recover_stuck_slicing_prep,
        recover_unclaimed_ready_batches,
        recover_unclaimed_ready_metadata_phases,
    )
    from app.services.user_settings_service import (
        recover_stale_dedup_jobs,
        recover_stale_reindex_job,
    )

    db = SessionLocal()
    try:
        orphaned = recover_orphaned_running_stages(db)
        dispatches = recover_stuck_pending_dispatches(db)
        batches = recover_stuck_batches(db)
        unclaimed = recover_unclaimed_ready_batches(db)
        unclaimed_metadata = recover_unclaimed_ready_metadata_phases(db)
        empty_batches = recover_empty_batches(db)
        stranded_embeddings = recover_embeddings_behind_failed_stage(db)
        slicing_prep = recover_stuck_slicing_prep(db)
        stale_reindex = recover_stale_reindex_job(db)
        stale_dedup = recover_stale_dedup_jobs(db)
        if stale_reindex or stale_dedup:
            db.commit()

        if stale_reindex:
            logger.info("recover_pipeline_task: flipped stale reindex_job to failed")
        if stale_dedup:
            logger.info(
                "recover_pipeline_task: flipped stale dedup_job(s) for case(s): %s",
                stale_dedup,
            )

        result = {
            "status": "success",
            "orphaned_docs": orphaned.get("docs_reset", 0),
            "orphaned_stages": orphaned.get("stages_reset", 0),
            "orphaned_stages_failed": orphaned.get("stages_failed", 0),
            "stuck_dispatches": dispatches.get("docs_redispatched", 0),
            "stuck_batches": batches.get("batches_recovered", 0),
            "unclaimed_batches": unclaimed.get("batches_dispatched", 0),
            "unclaimed_metadata_phases": unclaimed_metadata.get(
                "batches_dispatched", 0
            ),
            "empty_batches": empty_batches.get("batches_deleted", 0),
            "stranded_embeddings": stranded_embeddings.get("dispatched", 0)
            + stranded_embeddings.get("skipped", 0),
            "slicing_prep_recovered": len(slicing_prep.get("redispatched", [])),
            "slicing_prep_failed": len(slicing_prep.get("failed", [])),
            "stale_reindex": 1 if stale_reindex else 0,
            "stale_dedup": len(stale_dedup),
        }
        logger.info("recover_pipeline: %s", result)
        return result
    except Exception as e:
        logger.error(
            "recover_pipeline failed: %s", describe_exception(e), exc_info=True
        )
        return {"status": "error", "error": str(e)}
    finally:
        db.close()


# How long to wait after a failure before re-attempting an embed. Shorter
# than the schedule interval so a freshly-failed claim is eligible by the
# next tick, but long enough to avoid retrying the same claim seconds after
# the in-line attempt failed (which would just hit the same overloaded
# backend).
_RETRY_COOLDOWN_MINUTES = 5

# Cap claims processed per tick. Embeddings are cheap (~100ms) but a burst
# against a recovering LM Studio backend can re-trigger the load-failure
# cycle. 50 keeps the wave bounded and lets the next tick pick up the rest.
_RETRY_BATCH_SIZE = 50


@celery_app.task(name="app.tasks.maintenance.retry_failed_claim_embeddings_task")
def retry_failed_claim_embeddings_task():
    """Periodically retry claim embeddings whose previous attempt failed.

    Selects claims with `embedding_failed_at IS NOT NULL` older than the
    cooldown, ordered oldest-first (longest-failed get the best chance).
    Calls `upsert_claim_embedding` for each — that helper clears
    `embedding_failed_at` on success or re-sets it on continued failure,
    so this task is naturally idempotent and self-healing.

    Scheduled by `retry-failed-claim-embeddings` in celery_app.beat_schedule.
    """
    import asyncio

    from app.config import SessionLocal
    from app.models.database import Claim
    from app.services.claim_embedding import upsert_claim_embedding

    cutoff = datetime.now(UTC) - timedelta(minutes=_RETRY_COOLDOWN_MINUTES)
    db = SessionLocal()
    try:
        claim_ids = [
            row[0]
            for row in db.query(Claim.id)
            .filter(
                Claim.embedding_failed_at.isnot(None),
                Claim.embedding_failed_at < cutoff,
            )
            .order_by(Claim.embedding_failed_at.asc())
            .limit(_RETRY_BATCH_SIZE)
            .all()
        ]
    finally:
        db.close()

    if not claim_ids:
        return {"status": "success", "attempted": 0, "recovered": 0}

    logger.info("retry_failed_claim_embeddings: attempting %d claim(s)", len(claim_ids))
    recovered = 0
    for claim_id in claim_ids:
        db = SessionLocal()
        try:
            ok = asyncio.run(upsert_claim_embedding(claim_id, db))
            if ok:
                recovered += 1
        except Exception as e:
            # upsert_claim_embedding already logs the body summary at ERROR
            # via embed_claim_text — keep this fallback bare.
            logger.warning(
                "retry_failed_claim_embeddings: claim %d raised %s",
                claim_id,
                describe_exception(e),
            )
        finally:
            db.close()

    result = {
        "status": "success",
        "attempted": len(claim_ids),
        "recovered": recovered,
    }
    logger.info("retry_failed_claim_embeddings: %s", result)
    return result
