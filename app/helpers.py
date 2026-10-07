from sqlalchemy import or_
from sqlalchemy.orm import Session

from app.models.database import Case, CaseStatus, Document, IngestBatch
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


def build_sidebar_counts(db: Session, owner_id: int | None = None) -> dict:
    """Computes sidebar badge counts. When ``owner_id`` is given, the triage and
    case counts are scoped to that user (per-user triage inbox + visible cases)."""
    triage_count = triage_inbox_count(db, owner_id)
    total_docs = db.query(Document).count()

    case_q = db.query(Case).filter(Case.status != CaseStatus.CLOSED)
    if owner_id is not None:
        from app.models.database import User
        from app.services import access_service

        visible = access_service.visible_case_ids(db, db.get(User, owner_id))
        if visible is not None:
            case_q = case_q.filter(Case.id.in_(visible))
    case_count = case_q.count()
    # Lazy import: app.api.worker_queue → app.api.__init__ pulls route modules
    # that import helpers, so a top-level import here would cycle.
    from app.api.worker_queue import compute_queue_counts

    queue_counts = compute_queue_counts(db, owner_id=owner_id)
    return {
        "triage_count": triage_count,
        "total_docs": total_docs,
        "case_count": case_count,
        "queue_depth_count": queue_counts["n_executing"] + queue_counts["n_queued"],
        "queue_failed_count": queue_counts["n_failed"],
    }


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


def format_eur(value: float | None) -> str:
    """Formats a float as EUR with German-style punctuation: € 1.234,56"""
    if value is None:
        return "—"
    formatted = f"{value:,.2f}".replace(",", "X").replace(".", ",").replace("X", ".")
    return f"€\u00a0{formatted}"
