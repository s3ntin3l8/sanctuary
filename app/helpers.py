from sqlalchemy import or_
from sqlalchemy.orm import Session

from app.models.database import Document, IngestBatch
from app.models.enums import IngestBatchStatus


def triage_inbox_count(db: Session, owner_id: int | None = None) -> int:
    """Bundles awaiting triage (plus loose pre-batch docs), optionally per owner.

    Counts IngestBatches rather than documents, to stay consistent with the
    feed which groups docs into bundles. Loose docs without a batch
    (historical pre-batch data) are counted individually as fallback.
    """
    batch_q = db.query(IngestBatch).filter(
        IngestBatch.status != IngestBatchStatus.COMPLETED,
        IngestBatch.status != IngestBatchStatus.AWAITING_SLICING,
    )
    loose_q = db.query(Document).filter(
        Document.ingest_batch_id.is_(None),
        or_(Document.case_id == "_TRIAGE", Document.needs_review.is_(True)),
    )
    if owner_id is not None:
        batch_q = batch_q.filter(IngestBatch.owner_id == owner_id)
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
