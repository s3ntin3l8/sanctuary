"""4d — Thread-open close-out: flip thread_open=False once a reply relationship arrives."""

import logging

from sqlalchemy import text
from sqlalchemy.orm import Session

logger = logging.getLogger(__name__)


def scan_and_close_threads(db: Session) -> int:
    """Set thread_open=False on any doc that has a replies_to or references edge pointing at it.

    Returns the number of rows updated.
    Note: SAEnum stores enum .name (uppercase) — use uppercase literals in SQL.
    """
    result = db.execute(
        text(
            """
            UPDATE documents
            SET thread_open = 0
            WHERE thread_open = 1
              AND id IN (
                SELECT DISTINCT to_document_id
                FROM document_relationships
                WHERE relationship_type IN ('REPLIES_TO', 'REFERENCES')
              )
            """
        )
    )
    db.commit()
    updated = result.rowcount
    if updated:
        logger.info(f"Thread-open scanner closed {updated} thread(s)")
    return updated
