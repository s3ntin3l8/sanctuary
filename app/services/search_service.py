from collections.abc import Sequence

from sqlalchemy import or_
from sqlalchemy.orm import Session

from app.models.database import ActionItem, Case, Document, Entity, LegalCost
from app.models.enums import ActionItemType
from app.repositories.action_item import ActionItemRepository
from app.repositories.case import CaseRepository
from app.repositories.document import DocumentRepository
from app.repositories.entity import EntityRepository
from app.repositories.legal_cost import LegalCostRepository


class SearchResult:
    def __init__(
        self,
        cases: Sequence[Case] = [],
        documents: Sequence[Document] = [],
        deadlines: Sequence[ActionItem] = [],
        hearings: Sequence[ActionItem] = [],
        costs: Sequence[LegalCost] = [],
        entities: Sequence[Entity] = [],
    ):
        self.cases = cases
        self.documents = documents
        self.deadlines = deadlines
        self.hearings = hearings
        self.costs = costs
        self.entities = entities
        self.total = (
            len(cases)
            + len(documents)
            + len(deadlines)
            + len(hearings)
            + len(costs)
            + len(entities)
        )


class SearchService:
    """Service layer for unified search operations."""

    def __init__(self, db: Session):
        self.db = db
        self.case_repo = CaseRepository(db)
        self.doc_repo = DocumentRepository(db)
        self.action_repo = ActionItemRepository(db)
        self.cost_repo = LegalCostRepository(db)
        self.entity_repo = EntityRepository(db)

    def search_all(self, query: str, limit: int = 50) -> SearchResult:
        """Search across all entities."""
        query_like = f"%{query}%"

        cases = self._search_cases(query_like, limit // 6)
        documents = self._search_documents(query_like, limit // 6)
        deadlines = self._search_action_items(
            query_like, limit // 6, ActionItemType.DEADLINE
        )
        hearings = self._search_action_items(
            query_like, limit // 6, ActionItemType.COURT_DATE
        )
        costs = self._search_costs(query_like, limit // 6)

        return SearchResult(
            cases=cases,
            documents=documents,
            deadlines=deadlines,
            hearings=hearings,
            costs=costs,
        )

    def _search_cases(self, query: str, limit: int) -> Sequence[Case]:
        return (
            self.db.query(Case)
            .filter(or_(Case.id.ilike(query), Case.title.ilike(query)))
            .limit(limit)
            .all()
        )

    def _search_documents(self, query: str, limit: int) -> Sequence[Document]:
        return (
            self.db.query(Document)
            .filter(
                or_(
                    Document.title.ilike(query),
                    Document.content.ilike(query),
                    Document.sender.ilike(query),
                )
            )
            .limit(limit)
            .all()
        )

    def _search_action_items(
        self, query: str, limit: int, action_type: ActionItemType
    ) -> Sequence[ActionItem]:
        return (
            self.db.query(ActionItem)
            .filter(ActionItem.action_type == action_type)
            .filter(
                or_(
                    ActionItem.title.ilike(query),
                    ActionItem.description.ilike(query),
                )
            )
            .limit(limit)
            .all()
        )

    def _search_costs(self, query: str, limit: int) -> Sequence[LegalCost]:
        return (
            self.db.query(LegalCost)
            .filter(
                or_(
                    LegalCost.title.ilike(query),
                    LegalCost.rvg_position.ilike(query),
                    LegalCost.notes.ilike(query),
                )
            )
            .limit(limit)
            .all()
        )

    def get_activity_summary(self) -> dict:
        """Get activity summary for activity log."""
        recent_docs = self.doc_repo.get_recent(limit=20)
        pending_docs = self.doc_repo.get_pending_review()

        all_cases = self.case_repo.get_all()

        return {
            "recent_documents": recent_docs,
            "pending_documents": pending_docs,
            "all_cases": all_cases,
        }

    def get_dashboard_data(self) -> dict:
        """Get data for dashboard."""
        stats = self._get_case_stats()

        recent_docs = self.doc_repo.get_recent(limit=4)
        pending_docs = self.doc_repo.get_pending_review()

        upcoming_deadlines = self.action_repo.get_upcoming(
            days=7, action_type=ActionItemType.DEADLINE
        )
        upcoming_hearings = self.action_repo.get_upcoming(
            days=30, action_type=ActionItemType.COURT_DATE
        )

        return {
            "stats": stats,
            "recent_documents": recent_docs,
            "pending_documents": pending_docs[:4],
            "upcoming_deadlines": upcoming_deadlines[:4],
            "upcoming_hearings": upcoming_hearings[:3],
        }

    def _get_case_stats(self) -> dict:
        """Get case statistics."""
        from app.models.enums import CaseStatus

        all_cases = self.case_repo.get_all()
        active_cases = [c for c in all_cases if c.status != CaseStatus.CLOSED]

        return {
            "active_case_count": len(active_cases),
            "pending_review_count": self.doc_repo.count_pending_review(),
        }
