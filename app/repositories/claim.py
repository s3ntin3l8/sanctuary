from collections.abc import Sequence
from datetime import datetime

from sqlalchemy import func
from sqlalchemy.orm import Session

from app.models.database import Claim
from app.models.enums import ClaimStatus, ClaimType
from app.repositories.base import BaseRepository


class ClaimRepository(BaseRepository[Claim]):
    def __init__(self, db: Session):
        super().__init__(Claim, db)

    def get_by_case_count(self, case_ids: list[str]) -> dict[str, int]:
        """Bulk count claims by case IDs (avoids N+1)."""
        results = (
            self.db.query(Claim.case_id, func.count(Claim.id))
            .filter(Claim.case_id.in_(case_ids))
            .group_by(Claim.case_id)
            .all()
        )
        return dict(results)

    def create_claim(
        self,
        case_id: str,
        source_document_id: int,
        claim_text: str,
        claim_type: ClaimType,
        proceeding_id: int | None = None,
        status: ClaimStatus = ClaimStatus.ASSERTED,
        is_precedent: bool = False,
    ) -> Claim:
        return self.create(
            case_id=case_id,
            proceeding_id=proceeding_id,
            source_document_id=source_document_id,
            claim_text=claim_text,
            claim_type=claim_type,
            status=status,
            is_precedent=is_precedent,
            first_made_at=datetime.now(),
            last_updated_at=datetime.now(),
        )

    def get_open_in_case(self, case_id: str, limit: int = 20) -> Sequence[Claim]:
        return (
            self.db.query(Claim)
            .filter(
                Claim.case_id == case_id,
                Claim.status.in_([ClaimStatus.ASSERTED, ClaimStatus.CONTESTED]),
            )
            .order_by(Claim.last_updated_at.desc())
            .limit(limit)
            .all()
        )

    def update_status(self, claim_id: int, status: ClaimStatus) -> Claim | None:
        return self.update(claim_id, status=status, last_updated_at=datetime.now())
