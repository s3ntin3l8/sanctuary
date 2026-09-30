from datetime import datetime

from sqlalchemy.orm import Session

from app.core.timezone import now_utc
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

    def get_by_message_id(
        self, message_id: str, owner_id: int | None
    ) -> IngestBatch | None:
        """Scoped by owner_id: two users both ingesting the same email (e.g.
        both CC'd) must get their own batch, not collide into one — and the
        caller of this method deletes/replaces the returned row when it's an
        orphan, so an unscoped match here could delete another user's batch."""
        return (
            self.db.query(IngestBatch)
            .filter(
                IngestBatch.message_id == message_id,
                IngestBatch.owner_id == owner_id,
            )
            .first()
        )

    def get_by_source_hash(
        self, source_hash: str, owner_id: int | None
    ) -> IngestBatch | None:
        """Scoped by owner_id — same reasoning as get_by_message_id."""
        return (
            self.db.query(IngestBatch)
            .filter(
                IngestBatch.source_hash == source_hash,
                IngestBatch.owner_id == owner_id,
            )
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
            received_at=received_at or now_utc(),
            status=IngestBatchStatus.PENDING,
            ingest_date=now_utc(),
        )
