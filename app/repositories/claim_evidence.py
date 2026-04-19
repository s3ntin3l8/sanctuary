from datetime import datetime

from sqlalchemy.orm import Session

from app.models.database import ClaimEvidence
from app.models.enums import ClaimEvidenceRole, RelationshipConfidence
from app.repositories.base import BaseRepository


class ClaimEvidenceRepository(BaseRepository[ClaimEvidence]):
    def __init__(self, db: Session):
        super().__init__(ClaimEvidence, db)

    def link(
        self,
        claim_id: int,
        document_id: int,
        role: ClaimEvidenceRole,
        excerpt: str | None = None,
        confidence: RelationshipConfidence = RelationshipConfidence.AI_DETECTED,
    ) -> ClaimEvidence:
        return self.create(
            claim_id=claim_id,
            document_id=document_id,
            role=role,
            excerpt=excerpt,
            confidence=confidence,
            created_at=datetime.now(),
        )

    def evidence_exists(
        self, claim_id: int, document_id: int, role: ClaimEvidenceRole
    ) -> bool:
        return (
            self.db.query(ClaimEvidence)
            .filter(
                ClaimEvidence.claim_id == claim_id,
                ClaimEvidence.document_id == document_id,
                ClaimEvidence.role == role,
            )
            .first()
        ) is not None
