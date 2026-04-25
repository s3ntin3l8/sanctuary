"""4d — Thread-open close-out: keep `thread_open` consistent with user-confirmed edges.

Only USER_CONFIRMED edges count for thread closure. AI_DETECTED edges are suggestions
only — the user must confirm before a thread is considered resolved.

Source of truth for which document_types start a thread: `document_enricher.THREAD_OPEN_TYPES`.
"""

import logging

from sqlalchemy import text
from sqlalchemy.orm import Session

from app.models.database import Document, DocumentRelationship
from app.models.enums import RelationshipConfidence, RelationshipType
from app.services.intelligence.document_enricher import THREAD_OPEN_TYPES

logger = logging.getLogger(__name__)

_CLOSING_REL_TYPES = (RelationshipType.REPLIES_TO, RelationshipType.REFERENCES)


def recompute_thread_open(doc_id: int, db: Session) -> bool | None:
    """Recompute thread_open for one document from its USER_CONFIRMED edges.

    Returns the new thread_open value, or None if the document type doesn't
    participate in thread tracking. Commits the change if the value differs.
    """
    doc = db.query(Document).filter(Document.id == doc_id).first()
    if not doc or doc.document_type not in THREAD_OPEN_TYPES:
        return None

    has_confirmed_reply = (
        db.query(DocumentRelationship)
        .filter(
            DocumentRelationship.to_document_id == doc_id,
            DocumentRelationship.relationship_type.in_(_CLOSING_REL_TYPES),
            DocumentRelationship.confidence == RelationshipConfidence.USER_CONFIRMED,
        )
        .first()
        is not None
    )
    new_value = not has_confirmed_reply
    if doc.thread_open != new_value:
        doc.thread_open = new_value
        db.commit()
    return new_value


def scan_and_close_threads(db: Session) -> int:
    """Recompute thread_open from USER_CONFIRMED edges. Returns total rows changed.

    Note: SAEnum stores enum .name (uppercase) — use uppercase literals in SQL.
    """
    type_names_sql = ", ".join(f"'{t.name}'" for t in THREAD_OPEN_TYPES)

    closed = db.execute(
        text(
            f"""
            UPDATE documents
            SET thread_open = 0
            WHERE thread_open = 1
              AND document_type IN ({type_names_sql})
              AND id IN (
                SELECT DISTINCT to_document_id
                FROM document_relationships
                WHERE relationship_type IN ('REPLIES_TO', 'REFERENCES')
                  AND confidence = 'USER_CONFIRMED'
              )
            """
        )
    ).rowcount

    reopened = db.execute(
        text(
            f"""
            UPDATE documents
            SET thread_open = 1
            WHERE thread_open = 0
              AND document_type IN ({type_names_sql})
              AND id NOT IN (
                SELECT DISTINCT to_document_id
                FROM document_relationships
                WHERE relationship_type IN ('REPLIES_TO', 'REFERENCES')
                  AND confidence = 'USER_CONFIRMED'
              )
            """
        )
    ).rowcount

    db.commit()
    if closed or reopened:
        logger.info(f"Thread-open scanner: closed {closed}, reopened {reopened}")
    return closed + reopened
