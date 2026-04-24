import logging
from datetime import datetime
from typing import Any

from sqlalchemy import func
from sqlalchemy.orm import Session, joinedload

from app.models.database import ActionItem, Case, Document, Proceeding
from app.models.enums import (
    ActionItemStatus,
    ActionItemType,
    CaseStatus,
    Jurisdiction,
    ProceedingCourtLevel,
    ProceedingStatus,
    SignificanceTier,
)
from app.repositories.action_item import ActionItemRepository
from app.repositories.case import CaseRepository
from app.repositories.document import DocumentRepository
from app.repositories.entity import EntityRepository
from app.repositories.legal_cost import LegalCostRepository

logger = logging.getLogger(__name__)

DORMANCY_DAYS = 90


def _derive_case_title_from_subject(
    subject: str | None, internal_id: str
) -> str | None:
    """Derive a short case title from an email subject line."""
    if not subject:
        return None
    stripped = subject.lstrip()
    if stripped.startswith(internal_id):
        remainder = stripped[len(internal_id) :].lstrip(" -:/")
    else:
        remainder = subject
    for sep in (" vor dem ", " wg. ", " bzgl. ", " betr. "):
        idx = remainder.lower().find(sep)
        if idx != -1:
            remainder = remainder[:idx]
    return remainder.strip()[:80] or None


def get_or_create_case_from_reference(
    db: Session,
    internal_id: str,
    *,
    az_court: str | None = None,
    court_name: str | None = None,
    batch_subject: str | None = None,
    is_draft: bool = False,
) -> tuple[Case, Proceeding | None, bool]:
    """Return (case, proceeding, created).

    Race-safe: SELECT first, then INSERT only when missing. Never overwrites
    an existing case's is_draft flag. Caller is responsible for db.flush()/commit().
    """
    internal_id = internal_id.replace("/", "-").strip()
    existing = db.query(Case).filter(Case.id == internal_id).first()
    if existing:
        matched_proc = None
        if az_court:
            matched_proc = (
                db.query(Proceeding)
                .filter(
                    Proceeding.case_id == internal_id, Proceeding.az_court == az_court
                )
                .first()
            )
        return existing, matched_proc, False

    title = (
        _derive_case_title_from_subject(batch_subject, internal_id)
        or f"Neuer Fall {internal_id}"
    )
    new_case = Case(
        id=internal_id,
        title=title,
        status=CaseStatus.INTAKE,
        jurisdiction=Jurisdiction.DE,
        is_draft=is_draft,
    )
    db.add(new_case)
    db.flush()

    new_proc = None
    if az_court:
        new_proc = Proceeding(
            case_id=internal_id,
            az_court=az_court,
            court_name=court_name or "(Gericht folgt)",
            court_level=ProceedingCourtLevel.AG,
            status=ProceedingStatus.ACTIVE,
        )
        db.add(new_proc)
        db.flush()

    return new_case, new_proc, True


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

    def enrich_case_for_card(
        self, case: Case, now: datetime, last_home_visit: datetime | None = None
    ) -> dict[str, Any]:
        """Enrich a case with metadata needed for the dashboard/directory card."""
        from app.services.user_settings_service import count_new_since

        # Get closest action item
        next_action = (
            self.db.query(ActionItem)
            .filter(
                ActionItem.case_id == case.id,
                ActionItem.status == ActionItemStatus.OPEN,
            )
            .order_by(ActionItem.due_date.asc())
            .first()
        )

        new_docs_count = (
            count_new_since(case.id, last_home_visit, self.db) if last_home_visit else 0
        )

        # Days since last activity
        last_doc = (
            self.db.query(Document)
            .filter(Document.case_id == case.id)
            .order_by(Document.ingest_date.desc())
            .first()
        )
        days_since = (
            (now - last_doc.ingest_date).days
            if last_doc
            else (now - case.ingest_date).days
        )

        # Get active proceeding name
        active_proc = next((p for p in case.proceedings if p.status == "active"), None)
        if not active_proc and case.proceedings:
            active_proc = case.proceedings[0]

        proceeding_name = active_proc.court_name if active_proc else "General"

        # Max significance tier across most recent 20 documents
        _sig_rank = {
            SignificanceTier.CRITICAL: 4,
            SignificanceTier.SIGNIFICANT: 3,
            SignificanceTier.INFORMATIONAL: 2,
            SignificanceTier.ADMINISTRATIVE: 1,
        }
        recent_docs = (
            self.db.query(Document.significance_tier)
            .filter(Document.case_id == case.id, Document.significance_tier.isnot(None))
            .order_by(Document.ingest_date.desc())
            .limit(20)
            .all()
        )
        max_sig = max(
            (row[0] for row in recent_docs),
            key=lambda t: _sig_rank.get(t, 0),
            default=None,
        )

        return {
            "id": case.id,
            "title": case.title,
            "status": case.status,
            "status_line": case.ai_brief.get("status_line", "Active")
            if case.ai_brief
            else "Active",
            "next_action": next_action,
            "exposure_eur": case.total_cost_exposure / 100.0
            if case.total_cost_exposure
            else 0.0,
            "new_docs": new_docs_count,
            "days_since_activity": days_since,
            "tier": "delta" if new_docs_count > 0 else "normal",
            "proceeding_name": proceeding_name,
            "max_significance": max_sig,
        }

    def get_all_cases_directory(self) -> dict:
        """Get all cases with counts for directory view."""
        all_cases = self.case_repo.get_all_sorted_by_date()
        now = datetime.now()

        # Fetch last_home_visit from user settings for enrichment
        from app.models.database import UserSettings

        settings = self.db.query(UserSettings).first()
        last_home_visit_iso = (
            settings.settings_json.get("last_home_visit")
            if settings and settings.settings_json
            else None
        )
        last_home_visit = (
            datetime.fromisoformat(last_home_visit_iso) if last_home_visit_iso else None
        )

        enriched_cases = [
            self.enrich_case_for_card(c, now, last_home_visit) for c in all_cases
        ]

        active_cases = [c for c in enriched_cases if c["status"] != CaseStatus.CLOSED]
        closed_cases = [c for c in enriched_cases if c["status"] == CaseStatus.CLOSED]

        stats_by_status = self.case_repo.count_all_by_status()

        doc_counts = self.doc_repo.bulk_count_by_case([c.id for c in all_cases])
        action_counts = self.action_repo.bulk_count_open_by_case(
            [c.id for c in all_cases]
        )

        return {
            "cases": enriched_cases,
            "active_cases": active_cases,
            "closed_cases": closed_cases,
            "stats_by_status": stats_by_status,
            "doc_counts": doc_counts,
            "deadline_counts": action_counts,
            "total": len(all_cases),
        }

    def get_all_cases_directory_paginated(
        self, page: int = 1, per_page: int = 20
    ) -> dict:
        """Get paginated cases with counts for directory view."""
        cases, total = self.case_repo.get_paginated(page=page, per_page=per_page)
        now = datetime.now()

        # Fetch last_home_visit from user settings for enrichment
        from app.models.database import UserSettings

        settings = self.db.query(UserSettings).first()
        last_home_visit_iso = (
            settings.settings_json.get("last_home_visit")
            if settings and settings.settings_json
            else None
        )
        last_home_visit = (
            datetime.fromisoformat(last_home_visit_iso) if last_home_visit_iso else None
        )

        enriched_cases = [
            self.enrich_case_for_card(c, now, last_home_visit) for c in cases
        ]

        active_cases = [c for c in enriched_cases if c["status"] != CaseStatus.CLOSED]
        closed_cases = [c for c in enriched_cases if c["status"] == CaseStatus.CLOSED]

        stats_by_status = self.case_repo.count_all_by_status()

        case_ids = [c.id for c in cases]
        doc_counts = self.doc_repo.bulk_count_by_case(case_ids)
        action_counts = self.action_repo.bulk_count_open_by_case(case_ids)

        return {
            "cases": enriched_cases,
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
        case_id = case_id.replace("/", "-").strip()
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
            db.query(func.max(Document.ingest_date))
            .filter(Document.proceeding_id == proc.id)
            .scalar()
        )
        if last_activity is None:
            last_activity = proc.started_at or proc.ingest_date
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
