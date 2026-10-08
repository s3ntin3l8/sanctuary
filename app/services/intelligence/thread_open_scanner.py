"""4d — Thread-open close-out: keep `thread_open` consistent with trusted edges.

Two kinds of edge close a thread: user-asserted `replies_to`/`references`
(USER_CONFIRMED, or USER_CREATED — a link the user drew by hand), and EMAIL_HEADER
`replies_to` (the mail's own In-Reply-To names the target). AI_DETECTED edges are
suggestions only — the user must confirm before a thread is considered resolved.
EMAIL_HEADER `references` edges only say "same conversation", so they never close
a thread.

Source of truth for which document_types start a thread: `document_enricher.THREAD_OPEN_TYPES`.
"""

import logging
from typing import cast

from sqlalchemy import and_, or_, text
from sqlalchemy.engine import CursorResult
from sqlalchemy.orm import Session

from app.models.database import Document, DocumentRelationship
from app.models.enums import RelationshipConfidence, RelationshipType
from app.services.intelligence.document_enricher import THREAD_OPEN_TYPES

logger = logging.getLogger(__name__)

_CLOSING_REL_TYPES = (RelationshipType.REPLIES_TO, RelationshipType.REFERENCES)
_USER_ASSERTED = (
    RelationshipConfidence.USER_CONFIRMED,
    RelationshipConfidence.USER_CREATED,
)

# SQL twin of the ORM predicate in recompute_thread_open. SAEnum stores enum
# .name (uppercase), hence the uppercase literals.
_CLOSING_EDGE_SQL = """(
                    (relationship_type IN ('REPLIES_TO', 'REFERENCES')
                     AND confidence IN ('USER_CONFIRMED', 'USER_CREATED'))
                    OR (relationship_type = 'REPLIES_TO'
                        AND confidence = 'EMAIL_HEADER')
                  )"""


def recompute_thread_open(doc_id: int, db: Session) -> bool | None:
    """Recompute thread_open for one document from its closing edges.

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
            or_(
                and_(
                    DocumentRelationship.relationship_type.in_(_CLOSING_REL_TYPES),
                    DocumentRelationship.confidence.in_(_USER_ASSERTED),
                ),
                and_(
                    DocumentRelationship.relationship_type
                    == RelationshipType.REPLIES_TO,
                    DocumentRelationship.confidence
                    == RelationshipConfidence.EMAIL_HEADER,
                ),
            ),
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
    """Recompute thread_open from closing edges. Returns total rows changed.

    Note: SAEnum stores enum .name (uppercase) — use uppercase literals in SQL.
    """
    type_names_sql = ", ".join(f"'{t.name}'" for t in THREAD_OPEN_TYPES)

    closed = cast(
        CursorResult,
        db.execute(
            text(
                f"""
            UPDATE documents
            SET thread_open = false
            WHERE thread_open = true
              AND document_type IN ({type_names_sql})
              AND id IN (
                SELECT DISTINCT to_document_id
                FROM document_relationships
                WHERE {_CLOSING_EDGE_SQL}
              )
            """
            )
        ),
    ).rowcount

    reopened = cast(
        CursorResult,
        db.execute(
            text(
                f"""
            UPDATE documents
            SET thread_open = true
            WHERE thread_open = false
              AND document_type IN ({type_names_sql})
              AND id NOT IN (
                SELECT DISTINCT to_document_id
                FROM document_relationships
                WHERE {_CLOSING_EDGE_SQL}
              )
            """
            )
        ),
    ).rowcount

    db.commit()
    if closed or reopened:
        logger.info(f"Thread-open scanner: closed {closed}, reopened {reopened}")
    return closed + reopened
