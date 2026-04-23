from collections.abc import Sequence

from sqlalchemy.orm import Session, joinedload

from app.models.database import Document
from app.models.enums import OriginatorType
from app.repositories.document import DocumentRepository
from app.repositories.entity import EntityRepository
from app.services.ingestion import extract_case_id, extract_clean_title


class DocumentService:
    """Service layer for Document operations."""

    def __init__(self, db: Session):
        self.db = db
        self.doc_repo = DocumentRepository(db)
        self.entity_repo = EntityRepository(db)

    def get_document_with_context(self, doc_id: int) -> Document | None:
        """Get document with extraction context."""
        doc = self.doc_repo.get(doc_id)
        if not doc:
            return None
        return doc

    def get_triage_documents(self) -> Sequence[Document]:
        """Get documents in triage queue."""
        return self.doc_repo.get_triage_documents()

    def get_pending_review_documents(self) -> Sequence[Document]:
        """Get documents pending review."""
        return self.doc_repo.get_pending_review()

    def get_recent_documents(self, limit: int = 20) -> Sequence[Document]:
        """Get recent documents."""
        return self.doc_repo.get_recent(limit=limit)

    def get_documents_by_case(self, case_id: str) -> Sequence[Document]:
        """Get all documents for a case."""
        return self.doc_repo.get_by_case(case_id)

    def update_document_case(self, doc_id: int, case_id: str) -> Document | None:
        """Move document to a different case."""
        return self.doc_repo.update_case(doc_id, case_id)

    def resolve_triage(self, doc_id: int, case_id: str) -> Document | None:
        """Resolve triage document by assigning to a case."""
        doc = self.doc_repo.get(doc_id)
        if not doc:
            return None

        doc.case_id = case_id
        doc.needs_review = False
        doc.review_reasons = []

        self.db.flush()
        self.db.refresh(doc)
        return doc

    def delete_document(self, doc_id: int) -> bool:
        """Delete document and all dependent rows from the database and filesystem."""
        import os

        from sqlalchemy import or_, text

        from app.models.database import (
            ActionItem,
            Claim,
            ClaimEvidence,
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

        # Remove non-nullable FK dependents first (SQLite FK enforcement is off, but
        # explicit cleanup prevents orphan rows from being recalled by AI later).
        self.db.query(UserReaction).filter(UserReaction.document_id == doc_id).delete(
            synchronize_session=False
        )
        self.db.query(ActionItem).filter(ActionItem.document_id == doc_id).delete(
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

        # Nullable FKs: null out rather than delete the parent record.
        self.db.query(Claim).filter(Claim.source_document_id == doc_id).update(
            {"source_document_id": None}, synchronize_session=False
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
            self.db.commit()
            return True
        return False

    def get_all_senders(self) -> list[str]:
        """Get all unique senders."""
        return self.doc_repo.get_senders()

    def get_documents_by_sender(self, sender: str) -> Sequence[Document]:
        """Get documents from a specific sender."""
        return self.doc_repo.get_by_sender(sender)

    def search_documents(self, query: str) -> Sequence[Document]:
        """Search documents by title or content."""
        return self.doc_repo.search(query)

    def extract_and_update_metadata(self, doc_id: int) -> Document | None:
        """Re-extract metadata from document content."""
        doc = self.doc_repo.get(doc_id)
        if not doc or not doc.content:
            return doc

        case_result = extract_case_id(doc.title or "", doc.content)
        if case_result.get("value"):
            doc.case_id = case_result["value"]

        doc.title = extract_clean_title(doc.title or "", doc.content)

        self.db.flush()
        self.db.refresh(doc)
        return doc

    def get_contacts_data(self) -> dict:
        """Get all contacts with grouping for page rendering."""
        senders = self.get_all_senders()
        all_docs = self.doc_repo.get_all_with_sender()

        contacts = []
        for sender in senders:
            sender_docs = [d for d in all_docs if d.sender == sender]
            if sender_docs:
                originator = sender_docs[0].originator_type
                needs_review = sum(1 for d in sender_docs if d.needs_review)
                last_contact = max(
                    (d.created_at for d in sender_docs if d.created_at), default=None
                )
                contacts.append(
                    {
                        "name": sender,
                        "originator_type": originator,
                        "doc_count": len(sender_docs),
                        "needs_review_count": needs_review,
                        "last_contact": last_contact,
                        "case_ids": list({d.case_id for d in sender_docs if d.case_id}),
                    }
                )

        summary = {
            "total": len(contacts),
            "court": sum(
                1 for c in contacts if c["originator_type"] == OriginatorType.COURT
            ),
            "opposing": sum(
                1 for c in contacts if c["originator_type"] == OriginatorType.OPPOSING
            ),
            "own": sum(
                1 for c in contacts if c["originator_type"] == OriginatorType.OWN
            ),
        }

        return {
            "contacts": contacts,
            "documents": all_docs,
            "summary": summary,
        }

    def get_documents_paginated(
        self, cursor: int | None = None, limit: int = 20
    ) -> tuple:
        """Get paginated documents for timeline with cursor-based pagination."""
        query = (
            self.db.query(Document)
            .options(joinedload(Document.children))
            .order_by(Document.created_at.desc())
        )

        if cursor:
            cursor_doc = self.doc_repo.get(cursor)
            if cursor_doc:
                query = query.filter(Document.created_at < cursor_doc.created_at)

        docs = query.limit(limit + 1).all()
        has_more = len(docs) > limit
        if has_more:
            docs = docs[:limit]

        return docs, has_more
