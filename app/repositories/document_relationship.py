from collections.abc import Sequence
from datetime import datetime

from sqlalchemy import or_
from sqlalchemy.orm import Session, aliased

from app.models.database import DocumentRelationship
from app.models.enums import RelationshipConfidence, RelationshipType
from app.repositories.base import BaseRepository


class DocumentRelationshipRepository(BaseRepository[DocumentRelationship]):
    """Repository for typed N:N edges between documents."""

    def __init__(self, db: Session):
        super().__init__(DocumentRelationship, db)

    def get_outgoing(
        self,
        document_id: int,
        relationship_type: RelationshipType | None = None,
    ) -> Sequence[DocumentRelationship]:
        """Relationships where `document_id` is the source."""
        q = self.db.query(DocumentRelationship).filter(
            DocumentRelationship.from_document_id == document_id
        )
        if relationship_type is not None:
            q = q.filter(DocumentRelationship.relationship_type == relationship_type)
        return q.all()

    def get_incoming(
        self,
        document_id: int,
        relationship_type: RelationshipType | None = None,
    ) -> Sequence[DocumentRelationship]:
        """Relationships where `document_id` is the target."""
        q = self.db.query(DocumentRelationship).filter(
            DocumentRelationship.to_document_id == document_id
        )
        if relationship_type is not None:
            q = q.filter(DocumentRelationship.relationship_type == relationship_type)
        return q.all()

    def get_all_for_document(self, document_id: int) -> Sequence[DocumentRelationship]:
        return (
            self.db.query(DocumentRelationship)
            .filter(
                or_(
                    DocumentRelationship.from_document_id == document_id,
                    DocumentRelationship.to_document_id == document_id,
                )
            )
            .all()
        )

    def link(
        self,
        from_document_id: int,
        to_document_id: int,
        relationship_type: RelationshipType,
        confidence: RelationshipConfidence = RelationshipConfidence.AI_DETECTED,
        notes: str | None = None,
    ) -> DocumentRelationship:
        return self.create(
            from_document_id=from_document_id,
            to_document_id=to_document_id,
            relationship_type=relationship_type,
            confidence=confidence,
            notes=notes,
            created_at=datetime.now(),
        )

    def confirm(self, rel_id: int) -> DocumentRelationship | None:
        return self.update(rel_id, confidence=RelationshipConfidence.USER_CONFIRMED)

    def get_for_proceeding(self, proceeding_id: int) -> list:
        """Return all relationships where BOTH endpoints belong to the given proceeding."""
        from app.models.database import Document as Doc

        FromDoc = aliased(Doc)
        ToDoc = aliased(Doc)
        return (
            self.db.query(DocumentRelationship)
            .join(FromDoc, DocumentRelationship.from_document_id == FromDoc.id)
            .join(ToDoc, DocumentRelationship.to_document_id == ToDoc.id)
            .filter(
                FromDoc.proceeding_id == proceeding_id,
                ToDoc.proceeding_id == proceeding_id,
            )
            .all()
        )
