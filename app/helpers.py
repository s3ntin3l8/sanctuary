from sqlalchemy import exists, or_
from sqlalchemy.orm import Query, Session

from app.models.database import Document, IngestBatch
from app.models.enums import DocumentStatus, IngestBatchStatus


def awaiting_triage_batches(db: Session, owner_id: int | None = None) -> Query:
    """Batches that actually show up as bundles in the triage feed.

    The feed is built from documents, so a batch counts only if it is still open
    (not COMPLETED, not parked for slicing) *and* has at least one live
    document. A document-less batch (an email that produced nothing, a failed
    upload, a batch whose documents were all dismissed) is invisible in the feed
    and so must not inflate the rail badge, the home panel or notifications.
    """
    has_live_document = exists().where(
        Document.ingest_batch_id == IngestBatch.id,
        Document.status != DocumentStatus.DISMISSED,
    )
    q = db.query(IngestBatch).filter(
        IngestBatch.status.notin_(
            (IngestBatchStatus.COMPLETED, IngestBatchStatus.AWAITING_SLICING)
        ),
        has_live_document,
    )
    if owner_id is not None:
        q = q.filter(IngestBatch.owner_id == owner_id)
    return q


def triage_inbox_count(db: Session, owner_id: int | None = None) -> int:
    """Bundles awaiting triage (plus loose pre-batch docs), optionally per owner.

    Counts IngestBatches rather than documents, to stay consistent with the
    feed which groups docs into bundles. Loose docs without a batch
    (historical pre-batch data) are counted individually as fallback.
    """
    batch_q = awaiting_triage_batches(db, owner_id)
    loose_q = db.query(Document).filter(
        Document.ingest_batch_id.is_(None),
        or_(Document.case_id == "_TRIAGE", Document.confirmed_at.is_(None)),
    )
    if owner_id is not None:
        loose_q = loose_q.filter(Document.owner_id == owner_id)
    return batch_q.count() + loose_q.count()


def build_cost_summary(costs: list, CostStatus) -> dict:
    total_gross = sum(c.amount_gross or 0 for c in costs)
    total_paid = sum(c.amount_paid or 0 for c in costs)
    total_reimbursed = sum(c.amount_reimbursed or 0 for c in costs)
    total_outstanding = sum(
        (c.amount_gross or 0) - (c.amount_paid or 0)
        for c in costs
        if c.status not in (CostStatus.BEZAHLT, CostStatus.ERSTATTET)
    )
    total_reimbursable = sum(
        c.amount_gross - c.amount_reimbursed
        for c in costs
        if c.is_reimbursable and c.status not in (CostStatus.ERSTATTET,)
    )
    return {
        "total_gross": total_gross,
        "total_paid": total_paid,
        "total_reimbursed": total_reimbursed,
        "total_outstanding": total_outstanding,
        "total_reimbursable": total_reimbursable,
    }
