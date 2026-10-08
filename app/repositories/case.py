from collections.abc import Sequence

from sqlalchemy import func
from sqlalchemy.orm import Session

from app.core.timezone import now_utc
from app.models.database import Case
from app.models.enums import CaseStatus, Jurisdiction
from app.repositories.base import BaseRepository


class CaseRepository(BaseRepository[Case]):
    """Repository for Case operations."""

    def __init__(self, db: Session):
        super().__init__(Case, db)

    def get_by_id(self, case_id: str) -> Case | None:
        """Get case by its string ID."""
        return self.db.query(Case).filter(Case.id == case_id).first()

    def _escape_wildcards(self, s: str) -> str:
        """Escape SQL LIKE wildcards in user input."""
        return s.replace("\\", "\\\\").replace("%", r"\%").replace("_", r"\_")

    def get_all_active(self, include_drafts: bool = False) -> Sequence[Case]:
        """Get all non-closed cases."""
        query = self.db.query(Case).filter(Case.status != CaseStatus.CLOSED)
        if not include_drafts:
            query = query.filter(Case.is_draft.is_(False))
        return query.all()

    def get_all_sorted_by_title(self, include_drafts: bool = False) -> Sequence[Case]:
        """Get all cases sorted by title."""
        query = self.db.query(Case).filter(Case.id != "_TRIAGE")
        if not include_drafts:
            query = query.filter(Case.is_draft.is_(False))
        return query.order_by(Case.title.asc()).all()

    def list_for_picker(self, owner_id: int | None = None) -> Sequence[Case]:
        """Cases shown in the case-assignment picker (triage HUD, confirm modal).

        Excludes the `_TRIAGE` singleton and AI-created drafts. Sorted by title
        for stable rendering. Centralised here because the same query was open-
        coded in 6+ routes.

        When ``owner_id`` is given, restricted to that user's *editable* cases
        (owned ∪ EDITOR shares) — assigning a triage item to a case is a write
        to it, so the picker must never offer a case the user can only view.
        ``None`` (the default) matches the unrestricted/admin/no-user-context
        callers this method already had before per-user scoping existed.
        """
        query = self.db.query(Case).filter(
            Case.id != "_TRIAGE", Case.is_draft.is_(False)
        )
        if owner_id is not None:
            from app.models.database import User
            from app.services import access_service

            editable = access_service.editable_case_ids(
                self.db, self.db.get(User, owner_id)
            )
            if editable is not None:
                query = query.filter(Case.id.in_(editable))
        return query.order_by(Case.title.asc()).all()

    def get_all_sorted_by_date(
        self,
        descending: bool = True,
        include_drafts: bool = False,
        visible_ids: set[str] | None = None,
    ) -> Sequence[Case]:
        """Get all cases sorted by creation date.

        When ``visible_ids`` is given, restrict to that set (per-user isolation:
        owned ∪ shared). Pass ``None`` for the unrestricted/admin view.
        """
        query = self.db.query(Case).filter(Case.id != "_TRIAGE")
        if visible_ids is not None:
            query = query.filter(Case.id.in_(visible_ids))
        if not include_drafts:
            query = query.filter(Case.is_draft.is_(False))
        if descending:
            query = query.order_by(Case.ingest_date.desc())
        else:
            query = query.order_by(Case.ingest_date.asc())
        return query.all()

    def get_by_status(
        self, status: CaseStatus, include_drafts: bool = False
    ) -> Sequence[Case]:
        """Get cases by status."""
        query = self.db.query(Case).filter(Case.status == status)
        if not include_drafts:
            query = query.filter(Case.is_draft.is_(False))
        return query.all()

    def get_by_jurisdiction(
        self, jurisdiction: Jurisdiction, include_drafts: bool = False
    ) -> Sequence[Case]:
        """Get cases by jurisdiction."""
        query = self.db.query(Case).filter(Case.jurisdiction == jurisdiction)
        if not include_drafts:
            query = query.filter(Case.is_draft.is_(False))
        return query.all()

    def search(self, query: str, include_drafts: bool = False) -> Sequence[Case]:
        """Search cases by title or ID."""
        escaped = self._escape_wildcards(query.lower())
        query_pattern = f"%{escaped}%"
        db_query = self.db.query(Case).filter(
            (Case.id.ilike(query_pattern, escape="\\"))
            | (Case.title.ilike(query_pattern, escape="\\"))
        )
        if not include_drafts:
            db_query = db_query.filter(Case.is_draft.is_(False))
        return db_query.all()

    def count_by_status(self, status: CaseStatus) -> int:
        """Count cases by status."""
        return self.db.query(Case).filter(Case.status == status).count()

    def count_all_by_status(
        self, visible_ids: set[str] | None = None
    ) -> dict[CaseStatus, int]:
        """Count all cases grouped by status (single query, avoids N+1)."""
        query = self.db.query(Case.status, func.count()).filter(Case.id != "_TRIAGE")
        if visible_ids is not None:
            query = query.filter(Case.id.in_(visible_ids))
        results = query.group_by(Case.status).all()
        return {row[0]: row[1] for row in results}

    def create_case(
        self,
        case_id: str,
        title: str,
        status: CaseStatus = CaseStatus.INTAKE,
        jurisdiction: Jurisdiction = Jurisdiction.DE,
        owner_id: int | None = None,
    ) -> Case:
        """Create a new case."""
        return self.create(
            id=case_id,
            title=title,
            status=status,
            jurisdiction=jurisdiction,
            owner_id=owner_id,
            ingest_date=now_utc(),
        )

    def update_status(self, case_id: str, status: CaseStatus) -> Case | None:
        """Update case status."""
        case = self.get_by_id(case_id)
        if case:
            case.status = status
            if status == CaseStatus.CLOSED:
                case.closed_at = now_utc()
            self.db.flush()
            self.db.refresh(case)
        return case

    def exists(self, case_id: str) -> bool:  # type: ignore[override]  # CaseRepository intentionally specializes the generic base signature for Case-specific filters
        """Check if case exists."""
        return self.get_by_id(case_id) is not None

    def get_all(self) -> Sequence[Case]:  # type: ignore[override]  # CaseRepository intentionally specializes the generic base signature for Case-specific filters
        """Get all cases."""
        return self.db.query(Case).all()
