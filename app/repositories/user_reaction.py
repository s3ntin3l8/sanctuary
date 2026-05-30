from collections.abc import Sequence

from sqlalchemy.orm import Session

from app.models.database import Document, UserReaction
from app.models.enums import UserReactionType
from app.repositories.base import BaseRepository


class UserReactionRepository(BaseRepository[UserReaction]):
    """Repository for UserReaction operations.

    Reactions are high-weight strategic context the user captures during triage
    (🚩 Lies / ✅ True / 🔍 Needs Proof / ⚖️ Precedent). One reaction per
    (document, reaction_type) pair — toggling re-fires creates/deletes rather
    than stacking.
    """

    def __init__(self, db: Session):
        super().__init__(UserReaction, db)

    def get_by_document(self, document_id: int) -> Sequence[UserReaction]:
        return (
            self.db.query(UserReaction)
            .filter(UserReaction.document_id == document_id)
            .order_by(UserReaction.ingest_date.desc())
            .all()
        )

    def get_by_case(self, case_id: str) -> Sequence[UserReaction]:
        return (
            self.db.query(UserReaction)
            .join(Document, Document.id == UserReaction.document_id)
            .filter(Document.case_id == case_id)
            .order_by(UserReaction.ingest_date.desc())
            .all()
        )

    def find(self, document_id: int, reaction: UserReactionType) -> UserReaction | None:
        return (
            self.db.query(UserReaction)
            .filter(
                UserReaction.document_id == document_id,
                UserReaction.reaction == reaction,
            )
            .first()
        )

    def set_reaction(
        self,
        document_id: int,
        reaction: UserReactionType,
        notes: str | None = None,
        *,
        user_id: int,
    ) -> UserReaction:
        """Idempotent upsert — creates if absent, updates notes if present."""
        existing = self.find(document_id, reaction)
        if existing:
            if notes is not None:
                existing.notes = notes
                self.db.flush()
            return existing
        return self.create(
            document_id=document_id,
            reaction=reaction,
            notes=notes,
            user_id=user_id,
        )

    def get_by_document_ids(self, document_ids: list[int]) -> list[UserReaction]:
        if not document_ids:
            return []
        return (
            self.db.query(UserReaction)
            .filter(UserReaction.document_id.in_(document_ids))
            .order_by(UserReaction.ingest_date.desc())
            .all()
        )

    def clear_reaction(self, document_id: int, reaction: UserReactionType) -> bool:
        existing = self.find(document_id, reaction)
        if existing:
            self.db.delete(existing)
            self.db.flush()
            return True
        return False
