import logging
from datetime import datetime

from sqlalchemy import func
from sqlalchemy.orm import Session, joinedload

from app.models.database import Case, Document
from app.models.enums import (
    ActionItemStatus,
    ActionItemType,
    CaseStatus,
    Jurisdiction,
    ProceedingStatus,
)
from app.repositories.action_item import ActionItemRepository
from app.repositories.case import CaseRepository
from app.repositories.document import DocumentRepository
from app.repositories.entity import EntityRepository
from app.repositories.legal_cost import LegalCostRepository

logger = logging.getLogger(__name__)

DORMANCY_DAYS = 90


def recompute_total_cost_exposure(case_id: str, db: Session) -> int:
    """Recompute and persist Case.total_cost_exposure from doc.cost_delta values.

    Sums |cost_delta.amount| (in euros) across all non-TRIAGE documents for the
    case, stores as integer cents in Case.total_cost_exposure. Returns the new
    value in cents.
    """
    if not case_id or case_id == "_TRIAGE":
        return 0

    docs = (
        db.query(Document)
        .filter(
            Document.case_id == case_id,
            Document.cost_delta.isnot(None),
        )
        .all()
    )

    total_euros = 0.0
    for doc in docs:
        try:
            amount = (
                doc.cost_delta.get("amount")
                if isinstance(doc.cost_delta, dict)
                else None
            )
            if amount is not None:
                total_euros += abs(float(amount))
        except Exception:
            pass

    total_cents = int(round(total_euros * 100))

    case = db.query(Case).filter(Case.id == case_id).first()
    if case:
        case.total_cost_exposure = total_cents
        db.commit()
        logger.info(
            f"Case {case_id}: total_cost_exposure updated to {total_cents} cents"
        )

    return total_cents


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
        from app.services.user_settings_service import count_new_since, get_last_viewed

        case = self.case_repo.get_by_id(case_id)
        if not case:
            return None

        # Eager load proceedings to avoid N+1 in templates
        documents = self.doc_repo.get_by_case(
            case_id, options=[joinedload(Document.proceeding)]
        )
        deadlines = self.action_repo.get_by_case(
            case_id, action_type=ActionItemType.DEADLINE
        )
        hearings = self.action_repo.get_by_case(
            case_id, action_type=ActionItemType.COURT_DATE
        )
        costs = self.cost_repo.get_by_case(case_id)
        entities = self.entity_repo.get_by_case(case_id)

        last_visit = get_last_viewed(case_id, self.db)
        new_docs = count_new_since(case_id, last_visit, self.db)

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
            "last_visit": last_visit,
            "new_docs_since_last_visit": new_docs,
        }

    def get_all_cases_directory(self) -> dict:
        """Get all cases with counts for directory view."""
        all_cases = self.case_repo.get_all_sorted_by_date()

        active_cases = [c for c in all_cases if c.status != CaseStatus.CLOSED]
        closed_cases = [c for c in all_cases if c.status == CaseStatus.CLOSED]

        stats_by_status = self.case_repo.count_all_by_status()

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

        stats_by_status = self.case_repo.count_all_by_status()

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
    ) -> Case:
        """Create a new case."""
        return self.case_repo.create_case(
            case_id=case_id,
            title=title,
            status=status,
            jurisdiction=jurisdiction,
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


def _compute_dormancy_alert(case, db) -> str | None:
    """Return a textual alert when an active proceeding has been silent past the threshold."""
    now = datetime.now()
    active_procs = [
        p for p in (case.proceedings or []) if p.status == ProceedingStatus.ACTIVE
    ]
    if not active_procs:
        return None

    oldest_silent_proc = None
    oldest_days = 0

    for proc in active_procs:
        last_activity = (
            db.query(func.max(Document.created_at))
            .filter(Document.proceeding_id == proc.id)
            .scalar()
        )
        if last_activity is None:
            last_activity = proc.started_at or proc.created_at
        if last_activity is None:
            continue
        days = (now - last_activity).days
        if days > DORMANCY_DAYS and days > oldest_days:
            oldest_silent_proc = proc
            oldest_days = days

    if oldest_silent_proc is None:
        return None

    court = oldest_silent_proc.court_name or "Unknown court"
    az = oldest_silent_proc.az_court or "no docket"
    return f"{court} ({az}) has had no activity for {oldest_days} days."
