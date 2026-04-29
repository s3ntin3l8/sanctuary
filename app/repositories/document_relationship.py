from collections.abc import Sequence
from datetime import datetime

from sqlalchemy import or_
from sqlalchemy.orm import Session, aliased

from app.models.database import Document, DocumentRelationship
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
        """Idempotent: returns the existing edge if (from, to, type) already
        exists, otherwise creates a new one. AI re-runs would otherwise
        accumulate duplicate edges that the graph renders on top of each other.
        """
        existing = (
            self.db.query(DocumentRelationship)
            .filter(
                DocumentRelationship.from_document_id == from_document_id,
                DocumentRelationship.to_document_id == to_document_id,
                DocumentRelationship.relationship_type == relationship_type,
            )
            .first()
        )
        if existing:
            return existing
        return self.create(
            from_document_id=from_document_id,
            to_document_id=to_document_id,
            relationship_type=relationship_type,
            confidence=confidence,
            notes=notes,
            ingest_date=datetime.now(),
        )

    def confirm(self, rel_id: int) -> DocumentRelationship | None:
        return self.update(rel_id, confidence=RelationshipConfidence.USER_CONFIRMED)

    def get_for_proceeding(self, proceeding_id: int) -> list[DocumentRelationship]:
        """Return relationships where at least one endpoint belongs to the given proceeding.
        Used by the correspondence graph to show cross-proceeding references.
        """
        FromDoc = aliased(Document)
        ToDoc = aliased(Document)
        return (
            self.db.query(DocumentRelationship)
            .join(FromDoc, DocumentRelationship.from_document_id == FromDoc.id)
            .join(ToDoc, DocumentRelationship.to_document_id == ToDoc.id)
            .filter(
                or_(
                    FromDoc.proceeding_id == proceeding_id,
                    ToDoc.proceeding_id == proceeding_id,
                )
            )
            .all()
        )
