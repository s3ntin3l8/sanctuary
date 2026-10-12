"""Triage reactions (🚩/✅/🔍/⚖️) on a document.

Shared by the SPA route and the MCP layer. Callers check access and commit.
"""

from __future__ import annotations

from sqlalchemy.orm import Session

from app.models.database import Document, UserReaction
from app.models.enums import UserReactionType
from app.repositories.user_reaction import UserReactionRepository


def set_reaction(
    db: Session,
    user_id: int,
    doc: Document,
    reaction: UserReactionType,
    notes: str | None,
) -> list[UserReaction]:
    """Toggle ``reaction`` on ``doc``; with ``notes`` it is upserted instead.

    Returns the document's reactions as they stand afterwards. The caller commits.
    """
    repo = UserReactionRepository(db)
    existing = repo.find(doc.id, reaction)
    if existing and notes is None:
        db.delete(existing)
    else:
        repo.set_reaction(doc.id, reaction, notes, user_id=user_id)
    db.flush()
    return list(repo.get_by_document(doc.id))


def ensure_reaction(
    db: Session,
    user_id: int,
    doc: Document,
    reaction: UserReactionType,
    notes: str | None,
    *,
    agent_token_id: int | None = None,
) -> list[UserReaction]:
    """Make sure ``doc`` carries ``reaction`` (never removes one).

    Unlike :func:`set_reaction` this is idempotent: setting a reaction that is
    already there keeps it (with ``notes`` it updates them). Reactions are one
    per (document, type), so an agent re-asserting the user's own flag must not
    toggle it off. The caller commits.
    """
    repo = UserReactionRepository(db)
    repo.set_reaction(
        doc.id, reaction, notes, user_id=user_id, agent_token_id=agent_token_id
    )
    db.flush()
    return list(repo.get_by_document(doc.id))
