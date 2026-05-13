"""Pipeline sequencing helpers for Phase 4 task dispatch."""

import logging
from datetime import UTC, datetime

from sqlalchemy import text
from sqlalchemy.orm import Session

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
                SELECT 1 FROM documents
                WHERE ingest_batch_id = :batch_id
                  AND (
                    COALESCE(json_extract(pipeline_stages, '$.extract.status'), 'pending')
                        NOT IN ('completed', 'failed', 'skipped')
                    OR
                    COALESCE(json_extract(pipeline_stages, '$.metadata.status'), 'pending')
                        NOT IN ('completed', 'failed', 'skipped')
                  )
              )
            """
        ),
        {"now": datetime.now(UTC), "batch_id": batch_id},
    )
    db.commit()
    return result.rowcount == 1
