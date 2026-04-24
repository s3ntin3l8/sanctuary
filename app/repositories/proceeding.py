from collections.abc import Sequence
from datetime import datetime

from sqlalchemy.orm import Session

from app.models.database import Proceeding
from app.models.enums import ProceedingCourtLevel, ProceedingStatus
from app.repositories.base import BaseRepository


class ProceedingRepository(BaseRepository[Proceeding]):
    """Repository for Proceeding operations."""

    def __init__(self, db: Session):
        super().__init__(Proceeding, db)

    def get_by_case(self, case_id: str) -> Sequence[Proceeding]:
        return (
            self.db.query(Proceeding)
            .filter(Proceeding.case_id == case_id)
            .order_by(Proceeding.started_at.asc().nullsfirst())
            .all()
        )

    def get_paginated(
        self,
        page: int = 1,
        per_page: int = 20,
        case_id: str | None = None,
        status: ProceedingStatus | None = None,
    ) -> tuple[Sequence[Proceeding], int]:
        """Get paginated proceedings with total count."""
        query = self.db.query(Proceeding)

        if case_id:
            query = query.filter(Proceeding.case_id == case_id)

        if status:
            query = query.filter(Proceeding.status == status)

        total = query.count()

        proceedings = (
            query.order_by(Proceeding.started_at.asc().nullsfirst())
            .offset((page - 1) * per_page)
            .limit(per_page)
            .all()
        )

        return proceedings, total

    def get_active_by_case(self, case_id: str) -> Sequence[Proceeding]:
        return (
            self.db.query(Proceeding)
            .filter(Proceeding.case_id == case_id)
            .filter(Proceeding.status == ProceedingStatus.ACTIVE)
            .order_by(Proceeding.started_at.asc().nullsfirst())
            .all()
        )

    def get_by_az(self, az_court: str) -> Proceeding | None:
        return self.db.query(Proceeding).filter(Proceeding.az_court == az_court).first()

    def create_proceeding(
        self,
        case_id: str,
        court_name: str,
        court_level: ProceedingCourtLevel,
        subject_matter: str | None = None,
        az_court: str | None = None,
        started_at: datetime | None = None,
    ) -> Proceeding:
        return self.create(
            case_id=case_id,
            court_name=court_name,
            court_level=court_level,
            subject_matter=subject_matter,
            az_court=az_court,
            started_at=started_at,
            status=ProceedingStatus.ACTIVE,
            ingest_date=datetime.now(),
        )

    def close(self, proceeding_id: int) -> Proceeding | None:
        return self.update(
            proceeding_id,
            status=ProceedingStatus.CLOSED,
            ended_at=datetime.now(),
        )
