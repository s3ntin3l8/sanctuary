"""Build a prompt-ready summary of user reactions for a given case or document set.

Reactions (🚩 Lies / ✅ True / 🔍 Needs Proof / ⚖️ Precedent) are high-weight
strategic context captured during triage.  This module formats them so any AI
prompt can cite them back.
"""

from sqlalchemy.orm import Session

from app.models.database import Document, UserReaction
from app.models.enums import UserReactionType

_EMOJI = {
    UserReactionType.LIES: "🚩 Lies",
    UserReactionType.TRUE: "✅ True",
    UserReactionType.NEEDS_PROOF: "🔍 Needs Proof",
    UserReactionType.PRECEDENT: "⚖️ Precedent",
}


def format_reactions_for_case(db: Session, case_id: str) -> str:
    """Return a formatted block of user reactions for all documents in the case.

    Returns an empty string when there are no reactions (safe to skip from prompt).
    """
    reactions = (
        db.query(UserReaction)
        .join(Document, Document.id == UserReaction.document_id)
        .filter(Document.case_id == case_id)
        .order_by(UserReaction.created_at.asc())
        .all()
    )

    if not reactions:
        return ""

    doc_titles: dict[int, str] = {}
    for r in reactions:
        if r.document_id not in doc_titles:
            doc = db.get(Document, r.document_id)
            doc_titles[r.document_id] = doc.title if doc else f"doc #{r.document_id}"

    lines = [
        f"- Doc #{r.document_id} ({doc_titles[r.document_id]}): "
        f"{_EMOJI.get(r.reaction, str(r.reaction))}"
        + (f" — note: {r.notes}" if r.notes else "")
        for r in reactions
    ]

    return "User-flagged documents (high-weight context):\n" + "\n".join(lines)


def format_reactions_for_document(db: Session, document_id: int) -> str:
    """Return formatted reactions for a single document.

    Returns an empty string when there are no reactions.
    """
    reactions = (
        db.query(UserReaction)
        .filter(UserReaction.document_id == document_id)
        .order_by(UserReaction.created_at.asc())
        .all()
    )

    if not reactions:
        return ""

    lines = [
        f"- {_EMOJI.get(r.reaction, str(r.reaction))}"
        + (f" — note: {r.notes}" if r.notes else "")
        for r in reactions
    ]

    return "User reactions on this document:\n" + "\n".join(lines)
