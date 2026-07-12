from collections.abc import Sequence
from datetime import datetime, timedelta

from sqlalchemy import func
from sqlalchemy.orm import Session

from app.core.timezone import now_utc
from app.models.database import ActionItem
from app.models.enums import ActionItemStatus, ActionItemType
from app.repositories.base import BaseRepository


class ActionItemRepository(BaseRepository[ActionItem]):
    """Repository for ActionItem operations.

    Consolidates what used to be separate DeadlineRepository and HearingRepository.
    Filter by action_type to get a specific kind (deadline, court_date, etc.).
    """

    def __init__(self, db: Session):
        super().__init__(ActionItem, db)

    # --- case / proceeding scoped ------------------------------------------

    def get_by_case(
        self,
        case_id: str,
        action_type: ActionItemType | None = None,
    ) -> Sequence[ActionItem]:
        q = self.db.query(ActionItem).filter(ActionItem.case_id == case_id)
        if action_type is not None:
            q = q.filter(ActionItem.action_type == action_type)
        return q.order_by(ActionItem.due_date.asc()).all()

    def get_by_proceeding(self, proceeding_id: int) -> Sequence[ActionItem]:
        return (
            self.db.query(ActionItem)
            .filter(ActionItem.proceeding_id == proceeding_id)
            .order_by(ActionItem.due_date.asc())
            .all()
        )

    def get_by_source_document(self, document_id: int) -> Sequence[ActionItem]:
        return (
            self.db.query(ActionItem)
            .filter(ActionItem.source_document_id == document_id)
            .order_by(ActionItem.due_date.asc())
            .all()
        )

    # --- time-based ---------------------------------------------------------

    def get_upcoming(
        self,
        days: int = 7,
        action_type: ActionItemType | None = None,
    ) -> Sequence[ActionItem]:
        """Open action items due in the next `days` days."""
        now = now_utc()
        future = now + timedelta(days=days)
        q = (
            self.db.query(ActionItem)
            .filter(ActionItem.status == ActionItemStatus.OPEN)
            .filter(ActionItem.due_date >= now)
            .filter(ActionItem.due_date <= future)
        )
        if action_type is not None:
            q = q.filter(ActionItem.action_type == action_type)
        return q.order_by(ActionItem.due_date.asc()).all()

    def get_overdue(
        self,
        action_type: ActionItemType | None = None,
    ) -> Sequence[ActionItem]:
        now = now_utc()
        q = (
            self.db.query(ActionItem)
            .filter(ActionItem.status == ActionItemStatus.OPEN)
            .filter(ActionItem.due_date < now)
        )
        if action_type is not None:
            q = q.filter(ActionItem.action_type == action_type)
        return q.order_by(ActionItem.due_date.asc()).all()

    def get_open(
        self, action_type: ActionItemType | None = None
    ) -> Sequence[ActionItem]:
        q = self.db.query(ActionItem).filter(ActionItem.status == ActionItemStatus.OPEN)
        if action_type is not None:
            q = q.filter(ActionItem.action_type == action_type)
        return q.order_by(ActionItem.due_date.asc()).all()

    def get_completed(
        self, action_type: ActionItemType | None = None
    ) -> Sequence[ActionItem]:
        q = self.db.query(ActionItem).filter(
            ActionItem.status == ActionItemStatus.COMPLETED
        )
        if action_type is not None:
            q = q.filter(ActionItem.action_type == action_type)
        return q.order_by(ActionItem.due_date.desc()).all()

    # --- counts -------------------------------------------------------------

    def count_open_by_case(self, case_id: str) -> int:
        return (
            self.db.query(ActionItem)
            .filter(ActionItem.case_id == case_id)
            .filter(ActionItem.status == ActionItemStatus.OPEN)
            .count()
        )

    def bulk_count_open_by_case(self, case_ids: list[str]) -> dict[str, int]:
        """N+1-safe open-item counts across many cases."""
        results = (
            self.db.query(ActionItem.case_id, func.count(ActionItem.id))
            .filter(ActionItem.case_id.in_(case_ids))
            .filter(ActionItem.status == ActionItemStatus.OPEN)
            .group_by(ActionItem.case_id)
            .all()
        )
        return {row[0]: row[1] for row in results}

    # --- creation + transitions --------------------------------------------

    def create_action_item(
        self,
        case_id: str,
        title: str,
        due_date: datetime,
        action_type: ActionItemType = ActionItemType.DEADLINE,
        description: str | None = None,
        location: str | None = None,
        proceeding_id: int | None = None,
        source_document_id: int | None = None,
    ) -> ActionItem:
        return self.create(
            case_id=case_id,
            title=title,
            due_date=due_date,
            action_type=action_type,
            description=description,
            location=location,
            proceeding_id=proceeding_id,
            source_document_id=source_document_id,
            status=ActionItemStatus.OPEN,
            ingest_date=now_utc(),
        )

    def mark_completed(self, item_id: int) -> ActionItem | None:
        return self.update(item_id, status=ActionItemStatus.COMPLETED)

    def mark_open(self, item_id: int) -> ActionItem | None:
        return self.update(item_id, status=ActionItemStatus.OPEN)

    def mark_dismissed(self, item_id: int) -> ActionItem | None:
        return self.update(item_id, status=ActionItemStatus.DISMISSED)
