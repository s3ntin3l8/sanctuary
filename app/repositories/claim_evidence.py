from sqlalchemy.orm import Session

from app.core.timezone import now_utc
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
        agent_token_id: int | None = None,
    ) -> ClaimEvidence:
        return self.create(
            claim_id=claim_id,
            document_id=document_id,
            role=role,
            excerpt=excerpt,
            confidence=confidence,
            agent_token_id=agent_token_id,
            ingest_date=now_utc(),
        )

    def evidence_for(self, claim_id: int, document_id: int) -> ClaimEvidence | None:
        """The one evidence row a document holds on a claim, whatever its role."""
        return (
            self.db.query(ClaimEvidence)
            .filter(
                ClaimEvidence.claim_id == claim_id,
                ClaimEvidence.document_id == document_id,
            )
            .first()
        )
