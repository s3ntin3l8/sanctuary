from collections.abc import Sequence

from sqlalchemy.orm import Session

from app.models.database import Document
from app.models.enums import AuditEventType
from app.repositories.document import DocumentRepository
from app.services import audit_service


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
        from sqlalchemy import func, or_

        from app.core.paths import resolve_storage_path
        from app.models.database import (
            ActionItem,
            Claim,
            ClaimEvidence,
            Conversation,
            DocumentChunk,
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

        # Remove FK dependents first. UserReaction/DocumentPin's FK to documents
        # has no ON DELETE CASCADE, so the DB would reject the document delete
        # below if these still referenced it; ClaimEvidence does cascade, but
        # deleting it explicitly here too keeps this cleanup self-contained.
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
        # embedding lives on the document_chunks row itself, so deleting the
        # chunk rows drops their vectors too.
        self.db.query(DocumentChunk).filter(DocumentChunk.document_id == doc_id).delete(
            synchronize_session=False
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

        # Wave 2A: claims are global; case context comes from ClaimEvidence.
        # Deleting a document cascades to remove its ClaimEvidence rows
        # (FK ondelete=CASCADE). After that, any claim that's left with zero
        # evidence rows is rootless — no document anchors it to any case —
        # and should be deleted. Claims that still have evidence from OTHER
        # documents (e.g. a CONTESTS row from a later filing) stay; their
        # case scope comes from those remaining evidence anchors.
        self.db.query(ClaimEvidence).filter(ClaimEvidence.document_id == doc_id).delete(
            synchronize_session=False
        )

        rootless = (
            self.db.query(Claim.id)
            .outerjoin(ClaimEvidence, ClaimEvidence.claim_id == Claim.id)
            .group_by(Claim.id)
            .having(func.count(ClaimEvidence.id) == 0)
            .all()
        )
        rootless_ids = [c[0] for c in rootless]
        if rootless_ids:
            self.db.query(Claim).filter(Claim.id.in_(rootless_ids)).delete(
                synchronize_session=False
            )

        self.db.query(LegalCost).filter(LegalCost.source_document_id == doc_id).update(
            {"source_document_id": None}, synchronize_session=False
        )
        self.db.query(Entity).filter(Entity.source_document_id == doc_id).update(
            {"source_document_id": None}, synchronize_session=False
        )

        if self.doc_repo.delete(doc_id):
            resolved = resolve_storage_path(file_path) if file_path else None
            if resolved and resolved.exists():
                try:
                    resolved.unlink()
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
            audit_service.record(
                self.db,
                AuditEventType.DOCUMENT_DELETED,
                target_type="document",
                target_id=str(doc_id),
            )
            self.db.commit()
            return True
        return False
