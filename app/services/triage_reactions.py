"""Triage reactions + action item reads.

Free-function module (no class). Thin wrappers around UserReactionRepository
and ActionItemRepository. The bulk-read variant (get_reactions_by_doc_ids)
is the hot path — called once per feed render to populate reaction chips on
every bundle row.

get_action_items lives here rather than in triage_bundles.py because it's
called from per-doc render contexts (HUD, drawer) alongside the reaction
reads, not from the bundle hydration pass.
"""

from collections.abc import Sequence

from sqlalchemy.orm import Session

from app.models.database import UserReaction
from app.models.enums import UserReactionType
from app.repositories.action_item import ActionItemRepository
from app.repositories.user_reaction import UserReactionRepository


def get_reactions(db: Session, document_id: int) -> Sequence[UserReaction]:
    return UserReactionRepository(db).get_by_document(document_id)


def get_reactions_by_doc_ids(
    db: Session, document_ids: list[int]
) -> dict[int, set[UserReactionType]]:
    """Bulk variant of get_reactions for triage feed/bundle render.

    Returns ``{doc_id: {reaction, ...}}``. Docs with no reactions are absent
    from the dict — callers should default to ``set()``.
    """
    reactions = UserReactionRepository(db).get_by_document_ids(document_ids)
    out: dict[int, set[UserReactionType]] = {}
    for r in reactions:
        out.setdefault(r.document_id, set()).add(r.reaction)
    return out


def get_action_items(db: Session, document_id: int) -> list:
    return list(ActionItemRepository(db).get_by_source_document(document_id))


def toggle_reaction(
    db: Session,
    document_id: int,
    reaction: UserReactionType,
    notes: str | None = None,
) -> UserReaction | None:
    """Idempotent reaction set/clear.

    Create if absent (returns new row), delete if present and notes is None
    (returns None), update notes if present and notes is provided (returns
    updated row).
    """
    repo = UserReactionRepository(db)
    existing = repo.find(document_id, reaction)
    if existing and notes is None:
        repo.clear_reaction(document_id, reaction)
        db.commit()
        return None

    result = repo.set_reaction(document_id, reaction, notes)
    db.commit()
    return result


def clear_reaction(db: Session, document_id: int, reaction: UserReactionType) -> bool:
    repo = UserReactionRepository(db)
    cleared = repo.clear_reaction(document_id, reaction)
    if cleared:
        db.commit()
    return cleared
