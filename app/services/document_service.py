from collections.abc import Sequence

from sqlalchemy.orm import Session

from app.models.database import Document
from app.repositories.document import DocumentRepository


class DocumentService:
    """Service layer for Document operations.

    Scope is intentionally narrow: the cascade-aware delete and the contact
    page's by-sender lookup. Other CRUD goes through `DocumentRepository`
    directly — wrapping it here adds no value.
    """

    def __init__(self, db: Session):
        self.db = db
        self.doc_repo = DocumentRepository(db)

    def get_documents_by_sender(self, sender: str) -> Sequence[Document]:
        """Get documents from a specific sender."""
        return self.doc_repo.get_by_sender(sender)

    def delete_document(self, doc_id: int) -> bool:
        """Delete document and all dependent rows from the database and filesystem."""
        import os

        from sqlalchemy import or_, text

        from app.models.database import (
            ActionItem,
            Claim,
            ClaimEvidence,
            Conversation,
            DocumentPin,
            DocumentRelationship,
            Entity,
            LegalCost,
            UserReaction,
        )

        doc = self.doc_repo.get(doc_id)
        if not doc:
            return False

        file_path = doc.file_path
        ingest_batch_id = doc.ingest_batch_id

        # Remove non-nullable FK dependents first (SQLite FK enforcement is off, but
        # explicit cleanup prevents orphan rows from being recalled by AI later).
        self.db.query(UserReaction).filter(UserReaction.document_id == doc_id).delete(
            synchronize_session=False
        )
        self.db.query(DocumentPin).filter(DocumentPin.document_id == doc_id).delete(
            synchronize_session=False
        )
        self.db.query(ClaimEvidence).filter(ClaimEvidence.document_id == doc_id).delete(
            synchronize_session=False
        )
        self.db.query(DocumentRelationship).filter(
            or_(
                DocumentRelationship.from_document_id == doc_id,
                DocumentRelationship.to_document_id == doc_id,
            )
        ).delete(synchronize_session=False)
        self.db.execute(
            text("DELETE FROM document_vectors WHERE document_id = :id"),
            {"id": doc_id},
        )

        # Conversation.scope_id is a polymorphic string with no FK; clean up
        # any document-scoped chats so they aren't stranded.
        self.db.query(Conversation).filter(
            Conversation.scope_type == "document",
            Conversation.scope_id == str(doc_id),
        ).delete(synchronize_session=False)

        # Nullable FKs: null out rather than delete the parent record.
        self.db.query(ActionItem).filter(
            ActionItem.source_document_id == doc_id
        ).update({"source_document_id": None}, synchronize_session=False)

        # Remove Claims that originated from this document (and their evidence)
        # We must delete because source_document_id is NOT NULL on Claims.
        claims_to_delete = (
            self.db.query(Claim.id).filter(Claim.source_document_id == doc_id).all()
        )
        claim_ids = [c[0] for c in claims_to_delete]

        if claim_ids:
            self.db.query(ClaimEvidence).filter(
                ClaimEvidence.claim_id.in_(claim_ids)
            ).delete(synchronize_session=False)
            self.db.query(Claim).filter(Claim.id.in_(claim_ids)).delete(
                synchronize_session=False
            )

        self.db.query(LegalCost).filter(LegalCost.source_document_id == doc_id).update(
            {"source_document_id": None}, synchronize_session=False
        )
        self.db.query(Entity).filter(Entity.source_document_id == doc_id).update(
            {"source_document_id": None}, synchronize_session=False
        )

        if self.doc_repo.delete(doc_id):
            if file_path and os.path.exists(file_path):
                try:
                    os.remove(file_path)
                except Exception as e:
                    import logging

                    logging.getLogger(__name__).error(
                        f"Failed to delete file {file_path}: {e}"
                    )
            # Remove the batch if this was its last document
            if ingest_batch_id:
                from app.models.database import IngestBatch

                remaining = (
                    self.db.query(Document)
                    .filter(Document.ingest_batch_id == ingest_batch_id)
                    .count()
                )
                if remaining == 0:
                    self.db.query(IngestBatch).filter(
                        IngestBatch.id == ingest_batch_id
                    ).delete(synchronize_session=False)
            self.db.commit()
            return True
        return False
