"""Triage reactions bulk-read for feed/bundle render.

The HUD reaction routes write via UserReactionRepository directly; this
module's surface area is the single bulk-read variant called once per
triage feed/OOB render.
"""

from sqlalchemy.orm import Session

from app.models.enums import UserReactionType
from app.repositories.user_reaction import UserReactionRepository


def get_reactions_by_doc_ids(
    db: Session, document_ids: list[int]
) -> dict[int, set[UserReactionType]]:
    """Returns ``{doc_id: {reaction, ...}}``. Docs with no reactions are absent."""
    reactions = UserReactionRepository(db).get_by_document_ids(document_ids)
    out: dict[int, set[UserReactionType]] = {}
    for r in reactions:
        out.setdefault(r.document_id, set()).add(r.reaction)
    return out
