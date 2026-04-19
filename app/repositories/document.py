from collections.abc import Sequence
from datetime import datetime

from sqlalchemy import func, or_
from sqlalchemy.orm import Session

from app.models.database import Document
from app.models.enums import IngestStatus, OriginatorType, SignificanceTier
from app.repositories.base import BaseRepository


class DocumentRepository(BaseRepository[Document]):
    """Repository for Document operations."""

    def __init__(self, db: Session):
        super().__init__(Document, db)

    def get_by_case(self, case_id: str) -> Sequence[Document]:
        """Get all documents for a case."""
        return self.db.query(Document).filter(Document.case_id == case_id).all()

    def get_triage_documents(self) -> Sequence[Document]:
        """Get documents in triage inbox or needing review."""
        return (
            self.db.query(Document)
            .filter(or_(Document.case_id == "_TRIAGE", Document.needs_review))
            .order_by(Document.created_at.desc())
            .all()
        )

    def get_pending_review(self) -> Sequence[Document]:
        """Get documents needing review."""
        return (
            self.db.query(Document)
            .filter(Document.needs_review)
            .order_by(Document.created_at.desc())
            .all()
        )

    def get_by_sender(self, sender: str) -> Sequence[Document]:
        """Get documents from specific sender."""
        return (
            self.db.query(Document).filter(Document.sender.ilike(f"%{sender}%")).all()
        )

    def get_senders(self) -> list[str]:
        """Get unique senders."""
        return [
            r[0]
            for r in self.db.query(Document.sender)
            .filter(Document.sender.isnot(None))
            .distinct()
            .all()
        ]

    def get_all_with_sender(self) -> Sequence[Document]:
        """Get all documents with a sender."""
        return (
            self.db.query(Document)
            .filter(Document.sender.isnot(None))
            .order_by(Document.created_at.desc())
            .all()
        )

    def get_by_originator(self, originator: OriginatorType) -> Sequence[Document]:
        """Get documents by originator type."""
        return (
            self.db.query(Document).filter(Document.originator_type == originator).all()
        )

    def get_by_ingest_status(self, status: IngestStatus) -> Sequence[Document]:
        """Get documents by ingest status."""
        return self.db.query(Document).filter(Document.ingest_status == status).all()

    def search(self, query: str) -> Sequence[Document]:
        """Search documents by title or content."""
        query_lower = f"%{query.lower()}%"
        return (
            self.db.query(Document)
            .filter(
                or_(
                    Document.title.ilike(query_lower),
                    Document.content.ilike(query_lower),
                )
            )
            .all()
        )

    def get_recent(self, limit: int = 10) -> Sequence[Document]:
        """Get recently created documents."""
        return (
            self.db.query(Document)
            .order_by(Document.created_at.desc())
            .limit(limit)
            .all()
        )

    def get_since(self, since: datetime) -> Sequence[Document]:
        """Get documents created since given datetime."""
        return self.db.query(Document).filter(Document.created_at >= since).all()

    def get_children(self, parent_id: int) -> Sequence[Document]:
        """Get child documents."""
        return self.db.query(Document).filter(Document.parent_id == parent_id).all()

    def get_parent(self, doc_id: int) -> Document | None:
        """Get parent document."""
        doc = self.get(doc_id)
        if doc and doc.parent_id:
            return self.get(doc.parent_id)
        return None

    def count_by_case(self, case_id: str) -> int:
        """Count documents for a case."""
        return self.db.query(Document).filter(Document.case_id == case_id).count()

    def bulk_count_by_case(self, case_ids: list[str]) -> dict[str, int]:
        """Bulk count documents for multiple cases (fixes N+1)."""

        results = (
            self.db.query(Document.case_id, func.count(Document.id))
            .filter(Document.case_id.in_(case_ids))
            .group_by(Document.case_id)
            .all()
        )
        return dict(results)

    def count_pending_review(self) -> int:
        """Count documents needing review."""
        return self.db.query(Document).filter(Document.needs_review).count()

    def update_case(self, doc_id: int, case_id: str) -> Document | None:
        """Update document's case."""
        return self.update(doc_id, case_id=case_id, needs_review=False)

    def update_ingest_status(
        self,
        doc_id: int,
        status: IngestStatus,
        error: str | None = None,
    ) -> Document | None:
        """Update ingest status."""
        updates = {"ingest_status": status}
        if status == IngestStatus.PROCESSING:
            updates["ingest_started_at"] = datetime.now()
        elif status == IngestStatus.COMPLETED:
            updates["ingest_completed_at"] = datetime.now()
        elif status == IngestStatus.FAILED:
            updates["ingest_error"] = error
        return self.update(doc_id, **updates)

    def create_document(
        self,
        title: str,
        content: str = "",
        case_id: str | None = None,
        originator_type: OriginatorType = OriginatorType.UNKNOWN,
        sender: str | None = None,
        file_path: str | None = None,
    ) -> Document:
        """Create a new document."""
        return self.create(
            title=title,
            content=content,
            case_id=case_id,
            originator_type=originator_type,
            sender=sender,
            file_path=file_path,
            needs_review=case_id is None or case_id == "_TRIAGE",
            created_at=datetime.now(),
        )

    def list_prior_in_proceeding(
        self,
        proceeding_id: int,
        before_doc_id: int,
        tiers: list[SignificanceTier] | None = None,
        limit: int = 15,
    ) -> Sequence[Document]:
        """Prior docs in the same proceeding for relationship detection candidates."""
        q = self.db.query(Document).filter(
            Document.proceeding_id == proceeding_id,
            Document.id != before_doc_id,
        )
        if tiers:
            q = q.filter(Document.significance_tier.in_(tiers))
        return q.order_by(Document.received_date.desc().nullslast()).limit(limit).all()

    def get_paginated(
        self,
        page: int = 1,
        per_page: int = 20,
        case_id: str | None = None,
        needs_review: bool | None = None,
    ) -> tuple[Sequence[Document], int]:
        """Get paginated documents with total count."""
        query = self.db.query(Document)

        if case_id:
            query = query.filter(Document.case_id == case_id)

        if needs_review is not None:
            query = query.filter(Document.needs_review == needs_review)

        total = query.count()

        docs = (
            query.order_by(Document.created_at.desc())
            .offset((page - 1) * per_page)
            .limit(per_page)
            .all()
        )

        return docs, total
