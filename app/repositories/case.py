from collections.abc import Sequence
from datetime import datetime

from sqlalchemy import func
from sqlalchemy.orm import Session

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

    def get_all_sorted_by_date(
        self, descending: bool = True, include_drafts: bool = False
    ) -> Sequence[Case]:
        """Get all cases sorted by creation date."""
        query = self.db.query(Case).filter(Case.id != "_TRIAGE")
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

    def count_all_by_status(self) -> dict[CaseStatus, int]:
        """Count all cases grouped by status (single query, avoids N+1)."""
        results = (
            self.db.query(Case.status, func.count())
            .filter(Case.id != "_TRIAGE")
            .group_by(Case.status)
            .all()
        )
        return {row[0]: row[1] for row in results}

    def create_case(
        self,
        case_id: str,
        title: str,
        status: CaseStatus = CaseStatus.INTAKE,
        jurisdiction: Jurisdiction = Jurisdiction.DE,
    ) -> Case:
        """Create a new case."""
        return self.create(
            id=case_id,
            title=title,
            status=status,
            jurisdiction=jurisdiction,
            ingest_date=datetime.now(),
        )

    def update_status(self, case_id: str, status: CaseStatus) -> Case | None:
        """Update case status."""
        case = self.get_by_id(case_id)
        if case:
            case.status = status
            if status == CaseStatus.CLOSED:
                case.closed_at = datetime.now()
            self.db.flush()
            self.db.refresh(case)
        return case

    def exists(self, case_id: str) -> bool:
        """Check if case exists."""
        return self.get_by_id(case_id) is not None

    def get_all(self) -> Sequence[Case]:
        """Get all cases."""
        return self.db.query(Case).all()

    def get_paginated(
        self,
        page: int = 1,
        per_page: int = 20,
        status: CaseStatus | None = None,
        include_drafts: bool = False,
    ) -> tuple[Sequence[Case], int]:
        """Get paginated cases with total count."""
        query = self.db.query(Case).filter(Case.id != "_TRIAGE")

        if not include_drafts:
            query = query.filter(Case.is_draft.is_(False))

        if status:
            query = query.filter(Case.status == status)

        total = query.count()

        cases = (
            query.order_by(Case.ingest_date.desc())
            .offset((page - 1) * per_page)
            .limit(per_page)
            .all()
        )

        return cases, total
