"""Pipeline sequencing helpers for Phase 4 task dispatch."""

import logging
from datetime import UTC, datetime

from sqlalchemy import text
from sqlalchemy.orm import Session

from app.services.pipeline_status import _GATE_BLOCK_SKIP_REASONS

logger = logging.getLogger(__name__)


def claim_batch_for_analysis(batch_id: int, db: Session) -> bool:
    """Atomically claim a batch for analysis.

    Returns True if this call won the race (rowcount == 1), False otherwise.
    Uses a single UPDATE ... WHERE analysis_queued_at IS NULL to prevent
    duplicate analyze_batch_task dispatch when multiple workers complete
    the last two docs near-simultaneously.

    Readiness condition: every document in the batch has metadata stage
    completed or failed (i.e. Phase 1 is done for all docs).
    """
    result = db.execute(
        text(
            """
            UPDATE ingest_batches
            SET analysis_queued_at = :now
            WHERE id = :batch_id
              AND analysis_queued_at IS NULL
              AND NOT EXISTS (
                SELECT 1 FROM documents d2
                JOIN document_pipeline_stages dps2 ON dps2.document_id = d2.id
                WHERE d2.ingest_batch_id = :batch_id
                  AND dps2.stage = 'batch_analysis'
                  AND dps2.status IN ('completed', 'failed', 'skipped')
              )
              AND NOT EXISTS (
                SELECT 1 FROM documents
                WHERE ingest_batch_id = :batch_id
                  AND (
                    NOT EXISTS (
                        SELECT 1 FROM document_pipeline_stages dps
                        WHERE dps.document_id = documents.id
                          AND dps.stage = 'extract'
                          AND dps.status IN ('completed', 'failed', 'skipped')
                    )
                    OR
                    NOT EXISTS (
                        SELECT 1 FROM document_pipeline_stages dps
                        WHERE dps.document_id = documents.id
                          AND dps.stage = 'metadata'
                          AND dps.status IN ('completed', 'failed', 'skipped')
                    )
                  )
              )
            """
        ),
        {"now": datetime.now(UTC), "batch_id": batch_id},
    )
    db.commit()
    return result.rowcount == 1


def claim_batch_for_metadata_phase(batch_id: int, db: Session) -> bool:
    """Atomically claim a batch's metadata/chat phase — the OCR→chat barrier.

    Returns True if this call won the race (rowcount == 1); the winner is
    responsible for dispatching metadata_task for every doc in the batch at
    once, instead of each doc dispatching its own chat call the instant its
    own EXTRACT finishes. This is what keeps the shared inference host from
    swapping between the OCR model and the chat model mid-batch — see
    app/services/model_gate.py for the (now-redundant-within-a-batch, still
    useful cross-batch) drain-first heuristic this supersedes.

    Uses a single UPDATE ... WHERE metadata_phase_queued_at IS NULL to
    prevent duplicate dispatch when multiple workers finish the batch's last
    EXTRACT(s) near-simultaneously. Mirrors claim_batch_for_analysis.

    Readiness condition: every document in the batch has EXTRACT completed,
    failed, or skipped (a failed EXTRACT doesn't block the barrier — it just
    means that doc's own METADATA claim will no-op when dispatched).
    """
    result = db.execute(
        text(
            """
            UPDATE ingest_batches
            SET metadata_phase_queued_at = :now
            WHERE id = :batch_id
              AND metadata_phase_queued_at IS NULL
              AND NOT EXISTS (
                SELECT 1 FROM documents
                WHERE ingest_batch_id = :batch_id
                  AND NOT EXISTS (
                    SELECT 1 FROM document_pipeline_stages dps
                    WHERE dps.document_id = documents.id
                      AND dps.stage = 'extract'
                      AND dps.status IN ('completed', 'failed', 'skipped')
                  )
              )
            """
        ),
        {"now": datetime.now(UTC), "batch_id": batch_id},
    )
    db.commit()
    return result.rowcount == 1


def claim_case_brief_for_dispatch(case_id: str, db: Session) -> bool:
    """Atomically claim a case for brief dispatch when every doc in the case
    has CLAIMS in a terminal state (completed/failed/skipped-by-policy).

    Returns True only for the caller that won the claim (rowcount == 1); the
    winner is responsible for dispatching generate_case_brief_task. False
    means either the readiness predicate isn't satisfied yet (a sibling doc
    still has CLAIMS pending/running/retrying, or its CLAIMS was skipped
    because an upstream gate is still blocking) or another worker already
    claimed this wave.

    The "skipped" status alone isn't enough — SKIPPED can mean either
    "policy says this doc is ineligible" (terminal) or "gate not satisfied,
    we'll retry" (still in flight). The latter must NOT trigger the brief
    or it fires with empty claims data. See _GATE_BLOCK_SKIP_REASONS above.

    Mirrors claim_batch_for_analysis: single UPDATE ... WHERE brief_queued_at
    IS NULL collapses near-simultaneous dispatches when N docs in the same
    case complete CLAIMS within a few seconds of each other. The brief task
    clears brief_queued_at back to NULL on terminal exit so the next wave of
    pipeline activity can claim again.
    """
    # Build the SKIPPED reason filter inline — placeholders for safety.
    reason_params = {f"gateskip_{i}": r for i, r in enumerate(_GATE_BLOCK_SKIP_REASONS)}
    gate_skip_placeholders = ", ".join(f":{k}" for k in reason_params)
    result = db.execute(
        text(
            f"""
            UPDATE cases
            SET brief_queued_at = :now
            WHERE id = :case_id
              AND brief_queued_at IS NULL
              AND NOT EXISTS (
                SELECT 1 FROM documents
                WHERE case_id = :case_id
                  AND NOT EXISTS (
                    SELECT 1 FROM document_pipeline_stages dps
                    WHERE dps.document_id = documents.id
                      AND dps.stage = 'claims'
                      AND (
                        dps.status IN ('completed', 'failed')
                        OR (
                          dps.status = 'skipped'
                          AND (dps.reason IS NULL OR dps.reason NOT IN ({gate_skip_placeholders}))
                        )
                      )
                  )
              )
            """
        ),
        {"now": datetime.now(UTC), "case_id": case_id, **reason_params},
    )
    db.commit()
    return result.rowcount == 1


def release_case_brief_claim(case_id: str, db: Session) -> None:
    """Clear case.brief_queued_at so the next wave of pipeline activity can
    re-claim. Called by the brief task in a finally block on terminal exit
    (success or final failure — NOT on Celery retries between attempts)."""
    db.execute(
        text("UPDATE cases SET brief_queued_at = NULL WHERE id = :case_id"),
        {"case_id": case_id},
    )
    db.commit()
