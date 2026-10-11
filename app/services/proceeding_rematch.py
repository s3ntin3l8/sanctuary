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


def fill_proceeding_from_az(db: Session, doc: Document) -> bool:
    """Give a document without a proceeding the one its Aktenzeichen names.

    Deterministic, no AI: only an existing proceeding of the document's case
    with exactly that Aktenzeichen is used; nothing is created. Returns True
    when the document was assigned. Does not commit.
    """
    from app.services.ingestion.extractors import normalize_az_court

    if doc.proceeding_id or not doc.case_id or doc.case_id == "_TRIAGE":
        return False
    az = normalize_az_court(doc.az_court)
    if not az:
        return False
    target = (
        db.query(Proceeding)
        .filter(Proceeding.case_id == doc.case_id, Proceeding.az_court == az)
        .first()
    )
    if target is None:
        return False
    doc.proceeding_id = target.id
    db.flush()
    return True


def sync_batch_proceeding(db: Session, batch: IngestBatch | None) -> None:
    """Point the bundle at the proceeding its documents are in.

    The cover letter's proceeding wins, else the most common one among the
    active documents. A bundle whose documents have none keeps what it has.
    Does not commit.
    """
    from collections import Counter

    from app.models.enums import DocumentRole, DocumentStatus

    if batch is None:
        return
    rows = (
        db.query(Document.role, Document.proceeding_id)
        .filter(
            Document.ingest_batch_id == batch.id,
            Document.status != DocumentStatus.DISMISSED,
            Document.proceeding_id.isnot(None),
        )
        .all()
    )
    if not rows:
        return
    cover = [pid for role, pid in rows if role == DocumentRole.COVER_LETTER]
    pool = cover or [pid for _, pid in rows]
    batch.proceeding_id = Counter(pool).most_common(1)[0][0]
    db.flush()


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
    sync_batch_proceeding(
        db, db.get(IngestBatch, doc.ingest_batch_id) if doc.ingest_batch_id else None
    )

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
