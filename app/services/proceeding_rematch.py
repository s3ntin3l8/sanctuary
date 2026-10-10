"""Re-attach one document to the proceeding of its (user-corrected) Aktenzeichen."""

from sqlalchemy.orm import Session

from app.models.database import (
    ActionItem,
    Document,
    IngestBatch,
    LegalCost,
    Proceeding,
)
from app.models.enums import ProceedingCourtLevel, ProceedingStatus


def _is_unused_draft(proc: Proceeding, db: Session, own_batch_id: int | None) -> bool:
    """A draft nothing refers to any more — the same emptiness rule as
    ``CaseService.delete_empty_proceeding``. The document's own bundle does
    not count: it is repointed when the draft goes."""
    if not proc.is_draft:
        return False
    for model in (Document, ActionItem, LegalCost):
        if db.query(model).filter(model.proceeding_id == proc.id).first():
            return False
    others = db.query(IngestBatch).filter(IngestBatch.proceeding_id == proc.id)
    if own_batch_id is not None:
        others = others.filter(IngestBatch.id != own_batch_id)
    return others.first() is None


def rematch_proceeding(doc: Document, db: Session) -> None:
    """Move ``doc`` — and only ``doc`` — to the proceeding of ``doc.az_court``.

    Deterministic, no AI call. The proceeding of the case with that exact
    Aktenzeichen is reused (and reopened if an earlier run closed it), else one
    is created; a draft proceeding the move leaves empty is deleted. Does not
    commit.
    """
    if not doc.az_court or not doc.case_id or doc.case_id == "_TRIAGE":
        return

    old = db.get(Proceeding, doc.proceeding_id) if doc.proceeding_id else None
    if old is not None and old.az_court == doc.az_court:
        return

    target = (
        db.query(Proceeding)
        .filter(
            Proceeding.case_id == doc.case_id,
            Proceeding.az_court == doc.az_court,
        )
        .first()
    )
    if target is None:
        if old is not None and not old.az_court:
            old.az_court = doc.az_court
            return
        target = Proceeding(
            case_id=doc.case_id,
            court_name=old.court_name if old else "Unknown Court",
            court_level=old.court_level if old else ProceedingCourtLevel.AG,
            az_court=doc.az_court,
            status=ProceedingStatus.ACTIVE,
            is_draft=True,
        )
        db.add(target)
        db.flush()
    elif target.status == ProceedingStatus.CLOSED:
        target.status = ProceedingStatus.ACTIVE
        target.ended_at = None

    doc.proceeding_id = target.id
    db.flush()

    if (
        old is None
        or old.id == target.id
        or not _is_unused_draft(old, db, doc.ingest_batch_id)
    ):
        return
    # The bundle pointed at the draft only because this document seeded it.
    batch = db.get(IngestBatch, doc.ingest_batch_id) if doc.ingest_batch_id else None
    if batch is not None and batch.proceeding_id == old.id:
        batch.proceeding_id = target.id
        db.flush()
    db.delete(old)
