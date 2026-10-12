"""Passage-anchored margin pins on a document.

Shared by the SPA route and the MCP layer. Callers check access and commit.
"""

from __future__ import annotations

from sqlalchemy.orm import Session

from app.models.database import Document, DocumentPin
from app.repositories.document_pin import DocumentPinRepository
from app.services.case_dashboard_service import key_passages_for_template


class UnknownPassage(ValueError):
    """The passage id is not one of the document's key passages."""


def create_pin(
    db: Session, user_id: int, doc: Document, passage_id: str, note: str | None
) -> DocumentPin:
    """Anchor ``note`` to one of ``doc``'s key passages. The caller commits."""
    known = {p["id"] for p in key_passages_for_template(doc.key_passages or [])}
    if passage_id not in known:
        raise UnknownPassage(passage_id)
    return DocumentPinRepository(db).create(doc.id, passage_id, note, user_id=user_id)
