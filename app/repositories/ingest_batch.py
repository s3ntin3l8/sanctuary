from collections.abc import Sequence
from datetime import UTC, datetime

from sqlalchemy import text
from sqlalchemy.orm import Session

from app.models.database import IngestBatch
from app.models.enums import IngestBatchSourceType, IngestBatchStatus
from app.repositories.base import BaseRepository


class IngestBatchRepository(BaseRepository[IngestBatch]):
    """Repository for IngestBatch operations.

    One email (or scan session) = one batch. Documents belonging to the same
    batch form a family and are triaged together.
    """

    def __init__(self, db: Session):
        super().__init__(IngestBatch, db)

    def get_by_case(self, case_id: str) -> Sequence[IngestBatch]:
        return (
            self.db.query(IngestBatch)
            .filter(IngestBatch.case_id == case_id)
            .order_by(IngestBatch.received_at.desc())
            .all()
        )

    def get_pending(self) -> Sequence[IngestBatch]:
        return (
            self.db.query(IngestBatch)
            .filter(IngestBatch.status == IngestBatchStatus.PENDING)
            .order_by(IngestBatch.received_at.asc())
            .all()
        )

    def get_unassigned(self) -> Sequence[IngestBatch]:
        """Batches whose case_id hasn't been confirmed yet."""
        return (
            self.db.query(IngestBatch)
            .filter(IngestBatch.case_id.is_(None))
            .order_by(IngestBatch.received_at.desc())
            .all()
        )

    def get_by_message_id(self, message_id: str) -> IngestBatch | None:
        return (
            self.db.query(IngestBatch)
            .filter(IngestBatch.message_id == message_id)
            .first()
        )

    def get_by_source_hash(self, source_hash: str) -> IngestBatch | None:
        return (
            self.db.query(IngestBatch)
            .filter(IngestBatch.source_hash == source_hash)
            .first()
        )

    def create_batch(
        self,
        source_type: IngestBatchSourceType,
        sender_email: str | None = None,
        subject: str | None = None,
        raw_source_path: str | None = None,
        case_id: str | None = None,
        proceeding_id: int | None = None,
        received_at: datetime | None = None,
        owner_id: int | None = None,
    ) -> IngestBatch:
        return self.create(
            source_type=source_type,
            owner_id=owner_id,
            sender_email=sender_email,
            subject=subject,
            raw_source_path=raw_source_path,
            case_id=case_id,
            proceeding_id=proceeding_id,
            received_at=received_at or datetime.now(),
            status=IngestBatchStatus.PENDING,
            ingest_date=datetime.now(),
        )

    def assign_case(
        self,
        batch_id: int,
        case_id: str,
        proceeding_id: int | None = None,
    ) -> IngestBatch | None:
        return self.update(batch_id, case_id=case_id, proceeding_id=proceeding_id)

    def mark_completed(self, batch_id: int) -> IngestBatch | None:
        return self.update(batch_id, status=IngestBatchStatus.COMPLETED)

    def mark_failed(self, batch_id: int) -> IngestBatch | None:
        return self.update(batch_id, status=IngestBatchStatus.FAILED)

    def claim_for_analysis(self, batch_id: int) -> bool:
        """Atomically claim a batch for Phase 4 analysis.

        Returns True only if this call won the race (all docs done + first claim).
        Prevents duplicate analyze_batch_task dispatch under concurrent workers.
        """
        result = self.db.execute(
            text(
                """
                UPDATE ingest_batches
                SET analysis_queued_at = :now
                WHERE id = :batch_id
                  AND analysis_queued_at IS NULL
                  AND NOT EXISTS (
                    SELECT 1 FROM documents
                    WHERE ingest_batch_id = :batch_id
                      AND NOT EXISTS (
                        SELECT 1 FROM document_pipeline_stages dps2
                        WHERE dps2.document_id = documents.id
                          AND dps2.stage = 'metadata'
                          AND dps2.status IN ('completed', 'failed')
                      )
                  )
                """
            ),
            {"now": datetime.now(UTC), "batch_id": batch_id},
        )
        self.db.commit()
        return result.rowcount == 1
