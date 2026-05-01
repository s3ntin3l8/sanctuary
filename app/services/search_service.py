import logging
from collections.abc import Sequence

from sqlalchemy import or_, text
from sqlalchemy.orm import Session

logger = logging.getLogger(__name__)

from app.core.async_utils import run_async
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

    def _semantic_document_ids(self, query_text: str, k: int = 10) -> list[int]:
        """Return up to k document IDs ranked by vector similarity.

        Falls back to an empty list if the embedding model is unavailable or the
        query embedding fails for any reason.
        """
        from app.services.ai_config import get_embed_config
        from app.services.ai_provider import embed_provider
        from app.services.embeddings import _serialize

        embed_provider.reload_from_db(self.db)
        cfg = get_embed_config(self.db)

        try:
            import httpx

            params = run_async(
                embed_provider.get_embedding_params(cfg.embed_model, query_text)
            )
            with httpx.Client(timeout=10.0) as client:
                resp = client.post(
                    params["url"], json=params["json"], headers=params["headers"]
                )
                resp.raise_for_status()
                data = resp.json()
                embedding = data.get("embedding") or (
                    data.get("data", [{}])[0].get("embedding")
                    if data.get("data")
                    else None
                )

            if not embedding or len(embedding) != cfg.embed_dim:
                return []

            blob = _serialize(embedding)
            rows = self.db.execute(
                text(
                    "SELECT document_id, distance FROM document_vectors "
                    "WHERE embedding MATCH :blob ORDER BY distance LIMIT :k"
                ),
                {"blob": blob, "k": k},
            ).fetchall()
            return [row[0] for row in rows]
        except Exception as e:
            logger.debug(f"Semantic search unavailable: {e}")
            return []

    def semantic_document_search(
        self, query_text: str, limit: int = 10
    ) -> Sequence[Document]:
        """Return documents ranked by semantic similarity to query_text."""
        doc_ids = self._semantic_document_ids(query_text, k=limit)
        if not doc_ids:
            return []
        id_order = {doc_id: idx for idx, doc_id in enumerate(doc_ids)}
        docs = self.db.query(Document).filter(Document.id.in_(doc_ids)).all()
        docs.sort(key=lambda d: id_order.get(d.id, len(doc_ids)))
        return docs

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
