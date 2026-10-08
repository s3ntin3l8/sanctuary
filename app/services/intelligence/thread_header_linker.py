"""Deterministic reply edges from RFC 5322 In-Reply-To / References headers.

One email = one ``IngestBatch``; an edge connects the two batches' *lead*
documents (the node the case graph actually renders). No AI involved, so it runs
as soon as batch analysis has settled which document heads each batch.

Edge semantics, by what the header proves:

* ``In-Reply-To`` resolves to an ingested batch → ``REPLIES_TO`` (closes the
  parent's thread).
* Only an older ancestor from ``References`` resolves → ``REFERENCES`` to the
  nearest ingested one. Gmail import is allowlist/label scoped, so the user's own
  mail is usually not ingested and the real parent is often missing; claiming
  "replies to" the ancestor would be false. A header ``REFERENCES`` edge says
  "same conversation" and never closes a thread.

Both carry ``RelationshipConfidence.EMAIL_HEADER``. Direction matches the AI
detector: from = the reply, to = the earlier document.

Emails arrive in any order (concurrent Gmail import), so linking a batch also
re-resolves every batch that names it as an ancestor.
"""

import logging

from sqlalchemy.orm import Session

from app.models.database import Document, DocumentRelationship, IngestBatch
from app.models.enums import (
    DocumentRole,
    RelationshipConfidence,
    RelationshipType,
)
from app.repositories.document_relationship import insert_edge_if_absent
from app.repositories.ingest_batch import IngestBatchRepository
from app.services.intelligence.thread_open_scanner import recompute_thread_open

logger = logging.getLogger(__name__)

_HEADER_EDGE_TYPES = (RelationshipType.REPLIES_TO, RelationshipType.REFERENCES)
_NOTES = {
    RelationshipType.REPLIES_TO: "email header: In-Reply-To",
    RelationshipType.REFERENCES: "email header: References",
}


def lead_document(db: Session, batch_id: int) -> Document | None:
    """The document an inter-email edge should attach to, or None if ambiguous.

    Cover letters are bundle headers in the graph (their enclosures are not
    standalone nodes), so a single root cover letter wins. Otherwise the body
    document of an attachment-less email, otherwise the lone root document.
    """
    docs = (
        db.query(Document)
        .filter(Document.ingest_batch_id == batch_id)
        .order_by(Document.id)
        .all()
    )
    roots = [d for d in docs if d.parent_id is None]
    covers = [d for d in roots if d.role == DocumentRole.COVER_LETTER]
    if covers:
        return covers[0] if len(covers) == 1 else None
    bodies = [d for d in docs if (d.original_filename or "").startswith("email_body_")]
    if len(bodies) == 1:
        return bodies[0]
    if len(roots) == 1:
        return roots[0]
    logger.debug("Batch #%d: no unambiguous lead document", batch_id)
    return None


def _resolve_parent(
    db: Session, batch: IngestBatch
) -> tuple[Document, RelationshipType] | None:
    """Nearest ingested ancestor's lead document and the edge type it earns."""
    repo = IngestBatchRepository(db)
    # In-Reply-To is the immediate parent, so it wins over References when both
    # resolve; References are then tried newest to oldest (nearest ancestor).
    candidates: list[tuple[str, RelationshipType]] = []
    if batch.in_reply_to:
        candidates.append((batch.in_reply_to, RelationshipType.REPLIES_TO))
    for ref in reversed(batch.thread_refs or []):
        if ref != batch.in_reply_to:
            candidates.append((ref, RelationshipType.REFERENCES))

    for message_id, rel_type in candidates:
        parent = repo.get_by_message_id(message_id, batch.owner_id)
        if parent is None or parent.id == batch.id:
            continue
        lead = lead_document(db, parent.id)
        if lead is not None:
            return lead, rel_type
    return None


def _link_reply(db: Session, batch: IngestBatch, affected: set[int]) -> int:
    """Make this batch's EMAIL_HEADER edges match its headers. Returns inserts.

    ``affected`` collects documents whose incoming REPLIES_TO set changed, so
    the caller can recompute their ``thread_open``.
    """
    doc_ids = [
        row[0]
        for row in db.query(Document.id).filter(Document.ingest_batch_id == batch.id)
    ]
    if not doc_ids:
        return 0

    lead = lead_document(db, batch.id)
    resolved = _resolve_parent(db, batch) if lead is not None else None

    # Drop header edges that no longer reflect the current headers/leads (a
    # nearer ancestor arrived, or the lead document changed). Only this
    # provenance: AI and user edges are never touched.
    stale = (
        db.query(DocumentRelationship)
        .filter(
            DocumentRelationship.from_document_id.in_(doc_ids),
            DocumentRelationship.confidence == RelationshipConfidence.EMAIL_HEADER,
            DocumentRelationship.relationship_type.in_(_HEADER_EDGE_TYPES),
        )
        .all()
    )
    stale_ids: list[int] = []
    for rel in stale:
        if (
            lead is not None
            and resolved is not None
            and rel.from_document_id == lead.id
            and rel.to_document_id == resolved[0].id
            and rel.relationship_type == resolved[1]
        ):
            continue
        affected.add(rel.to_document_id)
        stale_ids.append(rel.id)
    if stale_ids:
        # Bulk DELETE rather than db.delete(): a concurrent linker that already
        # removed the same rows makes this a no-op instead of a StaleDataError.
        db.query(DocumentRelationship).filter(
            DocumentRelationship.id.in_(stale_ids)
        ).delete(synchronize_session=False)
    db.flush()

    if lead is None or resolved is None:
        return 0
    target, rel_type = resolved

    inserted = insert_edge_if_absent(
        db,
        from_document_id=lead.id,
        to_document_id=target.id,
        relationship_type=rel_type,
        confidence=RelationshipConfidence.EMAIL_HEADER,
        notes=_NOTES[rel_type],
    )
    if not inserted:
        # An AI suggestion for the same edge is superseded by the header fact;
        # USER_* rows stay as they are.
        existing = (
            db.query(DocumentRelationship)
            .filter(
                DocumentRelationship.from_document_id == lead.id,
                DocumentRelationship.to_document_id == target.id,
                DocumentRelationship.relationship_type == rel_type,
            )
            .one()
        )
        if existing.confidence == RelationshipConfidence.AI_DETECTED:
            existing.confidence = RelationshipConfidence.EMAIL_HEADER
            existing.notes = _NOTES[rel_type]
            inserted = True
            # The AI edge had flagged the source as "unresolved_relationship";
            # as a header fact it no longer does. Same transaction, so no commit.
            from app.services.ingestion.service import refresh_review_reasons

            db.flush()
            refresh_review_reasons(lead, db, commit=False)
    if inserted and rel_type == RelationshipType.REPLIES_TO:
        affected.add(target.id)
    return int(inserted)


def link_batch(db: Session, batch_id: int) -> int:
    """Create/refresh header-derived edges for a batch and for its descendants.

    Returns the number of edges written. Does not commit — except that
    ``recompute_thread_open`` commits when a thread flag actually flips.
    """
    batch = db.get(IngestBatch, batch_id)
    if batch is None:
        return 0

    affected: set[int] = set()
    written = 0
    if batch.in_reply_to or batch.thread_refs:
        written += _link_reply(db, batch, affected)

    # Out-of-order import: batches that already name this one as an ancestor
    # may now have a (nearer) resolvable parent.
    if batch.message_id:
        descendants = (
            db.query(IngestBatch)
            .filter(
                IngestBatch.owner_id == batch.owner_id,
                IngestBatch.id != batch.id,
                (IngestBatch.in_reply_to == batch.message_id)
                | IngestBatch.thread_refs.contains([batch.message_id]),
            )
            .all()
        )
        for child in descendants:
            written += _link_reply(db, child, affected)

    db.flush()
    for doc_id in affected:
        recompute_thread_open(doc_id, db)
    if written:
        logger.info("Batch #%d: wrote %d email-header edge(s)", batch_id, written)
    return written
