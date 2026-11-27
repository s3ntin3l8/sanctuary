"""Confirm or reject an AI-detected relationship between two documents.

Shared by ``POST /relationships/{id}/confirm`` / ``DELETE /relationships/{id}``
and the second-opinion confirm of a ``relationship`` finding. Access checks
are the caller's job. Both commit.
"""

from __future__ import annotations

from sqlalchemy.orm import Session

from app.models.database import Document, DocumentRelationship
from app.models.enums import RelationshipConfidence


def confirm(db: Session, rel: DocumentRelationship) -> None:
    """Promote an AI-detected relationship to user-confirmed; closes the target's thread.

    Email-header edges are already facts about the mail — there is nothing to
    confirm, and promoting one would turn a References-only link into a
    thread-closing claim. They can still be rejected.
    """
    from app.services.ingestion.service import refresh_review_reasons
    from app.services.intelligence.thread_open_scanner import recompute_thread_open

    if rel.confidence == RelationshipConfidence.EMAIL_HEADER:
        return
    source_id, target_id = rel.from_document_id, rel.to_document_id
    rel.confidence = RelationshipConfidence.USER_CONFIRMED
    db.commit()
    recompute_thread_open(target_id, db)
    source = db.get(Document, source_id)
    if source:
        refresh_review_reasons(source, db)


def reject(db: Session, rel: DocumentRelationship) -> None:
    """Drop a relationship and remember the rejection so later detection runs
    don't recreate it; reopens the target's thread if nothing confirmed remains."""
    from app.repositories.document_relationship import reject_edge
    from app.services.ingestion.service import refresh_review_reasons
    from app.services.intelligence.thread_open_scanner import recompute_thread_open

    source_id, target_id = rel.from_document_id, rel.to_document_id
    reject_edge(db, rel)
    db.commit()
    recompute_thread_open(target_id, db)
    source = db.get(Document, source_id)
    if source:
        refresh_review_reasons(source, db)
