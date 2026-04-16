from collections.abc import Sequence
from datetime import datetime

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

    def create_batch(
        self,
        source_type: IngestBatchSourceType,
        sender_email: str | None = None,
        subject: str | None = None,
        raw_source_path: str | None = None,
        case_id: str | None = None,
        proceeding_id: int | None = None,
        received_at: datetime | None = None,
    ) -> IngestBatch:
        return self.create(
            source_type=source_type,
            sender_email=sender_email,
            subject=subject,
            raw_source_path=raw_source_path,
            case_id=case_id,
            proceeding_id=proceeding_id,
            received_at=received_at or datetime.now(),
            status=IngestBatchStatus.PENDING,
            created_at=datetime.now(),
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
