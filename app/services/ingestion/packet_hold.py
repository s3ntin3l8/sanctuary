"""Hold an email attachment that looks like several letters until the user decides.

EXTRACT notes the pages where another letter seems to begin (``meta.packet_starts``,
``service._flag_packet``). For an email batch that is not just a hint: the document
is held (``Document.held_for_split_at``) and the whole batch waits at the OCR->chat
barrier, so no AI stage reads a packet as if it were one letter. The user then
splits it (the parts replace the document, see ``slicing_service``) or keeps it as
one (``release(..., kept=True)``); either way the batch moves on.

Manual and MCP uploads, scans and the parts of a split are never held.
"""

from __future__ import annotations

import logging

from sqlalchemy.orm import Session

from app.core.timezone import now_utc
from app.models.database import Document, IngestBatch
from app.models.enums import IngestBatchSourceType

logger = logging.getLogger(__name__)


def apply(doc: Document, db: Session) -> bool:
    """Hold ``doc`` when its extraction found packet starts. Returns whether it is held.

    Needs: starts found, the switch on, an email batch, no earlier "keep as one"
    and no ``slice_range`` (a part of a split). Commits nothing: the caller's
    extraction commit makes the hold visible before EXTRACT completes, so no
    sibling can win the barrier in between.
    """
    from app.services.user_settings_service import get_hold_packets

    meta = doc.meta or {}
    if (
        not meta.get("packet_starts")
        or meta.get("packet_kept")
        or meta.get("slice_range")
        or doc.ingest_batch_id is None
        or not get_hold_packets(db)
    ):
        return False
    batch = db.get(IngestBatch, doc.ingest_batch_id)
    if batch is None or batch.source_type != IngestBatchSourceType.EMAIL:
        return False
    doc.held_for_split_at = now_utc()
    return True


def batch_is_held(db: Session, batch_id: int | None) -> bool:
    """True while any document of the batch awaits a split decision."""
    if batch_id is None:
        return False
    return (
        db.query(Document.id)
        .filter(
            Document.ingest_batch_id == batch_id,
            Document.held_for_split_at.is_not(None),
        )
        .first()
        is not None
    )


def held_batch_ids(db: Session) -> set[int]:
    """Every batch with a document awaiting a split decision."""
    rows = (
        db.query(Document.ingest_batch_id)
        .filter(
            Document.ingest_batch_id.is_not(None),
            Document.held_for_split_at.is_not(None),
        )
        .distinct()
        .all()
    )
    return {batch_id for (batch_id,) in rows if batch_id is not None}


def release(db: Session, doc: Document, *, kept: bool = False) -> None:
    """End ``doc``'s hold; ``kept`` remembers the user chose to keep it as one.

    Idempotent. Once no document of the batch is held any more, the OCR->chat
    barrier is poked so the batch continues without waiting for a sweep.
    """
    if doc.held_for_split_at is None and not kept:
        return
    doc.held_for_split_at = None
    if kept:
        doc.meta = {**(doc.meta or {}), "packet_kept": True}
    batch_id = doc.ingest_batch_id
    db.commit()
    if batch_id is not None:
        resume_batch(db, batch_id)


def release_all(db: Session) -> int:
    """End every hold (the switch was turned off). Returns how many documents."""
    held = db.query(Document).filter(Document.held_for_split_at.is_not(None)).all()
    batch_ids = {d.ingest_batch_id for d in held if d.ingest_batch_id is not None}
    for doc in held:
        doc.held_for_split_at = None
    db.commit()
    for batch_id in sorted(batch_ids):
        resume_batch(db, batch_id)
    return len(held)


def resume_batch(db: Session, batch_id: int) -> None:
    """Claim the batch's OCR->chat barrier and dispatch METADATA, if nothing holds it.

    The claim itself refuses while a document is held or still extracting, so this
    is safe to call after every release.
    """
    from app.services.intelligence.orchestrator import claim_batch_for_metadata_phase
    from app.tasks.dispatch import dispatch_task
    from app.tasks.document_processing import metadata_task

    if not claim_batch_for_metadata_phase(batch_id, db):
        return
    doc_ids = [
        row[0]
        for row in db.query(Document.id)
        .filter(Document.ingest_batch_id == batch_id)
        .all()
    ]
    for doc_id in doc_ids:
        dispatch_task(metadata_task, doc_id)
    logger.info(
        "Batch %d: released from packet hold (%d documents)", batch_id, len(doc_ids)
    )
