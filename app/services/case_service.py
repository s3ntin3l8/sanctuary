from datetime import datetime

from sqlalchemy.orm import Session

from app.models.database import Case, Document
from app.models.enums import ActionItemStatus, ActionItemType, CaseStatus, Jurisdiction
from app.repositories.action_item import ActionItemRepository
from app.repositories.case import CaseRepository
from app.repositories.document import DocumentRepository
from app.repositories.entity import EntityRepository
from app.repositories.legal_cost import LegalCostRepository


class CaseService:
    """Service layer for Case operations."""

    def __init__(self, db: Session):
        self.db = db
        self.case_repo = CaseRepository(db)
        self.doc_repo = DocumentRepository(db)
        self.action_repo = ActionItemRepository(db)
        self.entity_repo = EntityRepository(db)
        self.cost_repo = LegalCostRepository(db)

    def get_case_with_summary(self, case_id: str) -> dict | None:
        """Get case with all related data."""
        case = self.case_repo.get_by_id(case_id)
        if not case:
            return None

        documents = self.doc_repo.get_by_case(case_id)
        deadlines = self.action_repo.get_by_case(
            case_id, action_type=ActionItemType.DEADLINE
        )
        hearings = self.action_repo.get_by_case(
            case_id, action_type=ActionItemType.COURT_DATE
        )
        costs = self.cost_repo.get_by_case(case_id)
        entities = self.entity_repo.get_by_case(case_id)

        now = datetime.now()
        return {
            "case": case,
            "documents": documents,
            "deadlines": deadlines,
            "hearings": hearings,
            "costs": costs,
            "entities": entities,
            "document_count": len(documents),
            "pending_review_count": sum(1 for d in documents if d.needs_review),
            "upcoming_deadlines": sum(
                1 for d in deadlines if d.status == ActionItemStatus.OPEN
            ),
            "upcoming_hearings": sum(1 for h in hearings if h.due_date > now),
        }

    def get_all_cases_directory(self) -> dict:
        """Get all cases with counts for directory view."""
        all_cases = self.case_repo.get_all_sorted_by_date()

        active_cases = [c for c in all_cases if c.status != CaseStatus.CLOSED]
        closed_cases = [c for c in all_cases if c.status == CaseStatus.CLOSED]

        stats_by_status = {}
        for status in CaseStatus:
            stats_by_status[status] = self.case_repo.count_by_status(status)

        doc_counts = self.doc_repo.bulk_count_by_case([c.id for c in all_cases])
        action_counts = self.action_repo.bulk_count_open_by_case(
            [c.id for c in all_cases]
        )

        return {
            "cases": all_cases,
            "active_cases": active_cases,
            "closed_cases": closed_cases,
            "stats_by_status": stats_by_status,
            "doc_counts": doc_counts,
            "deadline_counts": action_counts,  # kept key name for template compatibility
            "total": len(all_cases),
        }

    def get_all_cases_directory_paginated(
        self, page: int = 1, per_page: int = 20
    ) -> dict:
        """Get paginated cases with counts for directory view."""
        cases, total = self.case_repo.get_paginated(page=page, per_page=per_page)

        active_cases = [c for c in cases if c.status != CaseStatus.CLOSED]
        closed_cases = [c for c in cases if c.status == CaseStatus.CLOSED]

        stats_by_status = {}
        for status in CaseStatus:
            stats_by_status[status] = self.case_repo.count_by_status(status)

        case_ids = [c.id for c in cases]
        doc_counts = self.doc_repo.bulk_count_by_case(case_ids)
        action_counts = self.action_repo.bulk_count_open_by_case(case_ids)

        return {
            "cases": cases,
            "active_cases": active_cases,
            "closed_cases": closed_cases,
            "stats_by_status": stats_by_status,
            "doc_counts": doc_counts,
            "deadline_counts": action_counts,
            "total": total,
            "page": page,
            "per_page": per_page,
            "total_pages": (total + per_page - 1) // per_page if total > 0 else 1,
        }

    def create_case(
        self,
        case_id: str,
        title: str,
        status: CaseStatus = CaseStatus.INTAKE,
        jurisdiction: Jurisdiction = Jurisdiction.DE,
        court_id: str | None = None,
    ) -> Case:
        """Create a new case."""
        return self.case_repo.create_case(
            case_id=case_id,
            title=title,
            status=status,
            jurisdiction=jurisdiction,
            court_id=court_id,
        )

    def update_case_status(self, case_id: str, status: CaseStatus) -> Case | None:
        """Update case status."""
        return self.case_repo.update_status(case_id, status)

    def delete_case(self, case_id: str) -> bool:
        """Delete a case and all related data."""
        if case_id == "_TRIAGE":
            return False

        self.doc_repo.update(case_id, case_id=None)
        self.entity_repo.delete_by_case(case_id)
        for cost in self.cost_repo.get_by_case(case_id):
            self.cost_repo.delete(cost.id)
        for item in self.action_repo.get_by_case(case_id):
            self.action_repo.delete(item.id)

        return self.case_repo.delete(case_id)

    def get_dashboard_stats(self) -> dict:
        """Get statistics for dashboard."""
        all_cases = self.case_repo.get_all()
        active_cases = [c for c in all_cases if c.status != CaseStatus.CLOSED]

        pending_docs = self.doc_repo.get_pending_review()

        court_doc_count = (
            self.db.query(Document)
            .filter(Document.originator_type.in_(["court"]))
            .count()
        )

        upcoming_deadlines = self.action_repo.get_upcoming(
            days=7, action_type=ActionItemType.DEADLINE
        )
        upcoming_hearings = self.action_repo.get_upcoming(
            days=30, action_type=ActionItemType.COURT_DATE
        )

        return {
            "active_case_count": len(active_cases),
            "pending_review_count": len(pending_docs),
            "court_doc_count": court_doc_count,
            "upcoming_deadlines": upcoming_deadlines,
            "upcoming_hearings": upcoming_hearings,
        }
