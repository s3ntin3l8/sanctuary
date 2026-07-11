"""Triage confirmation flow + post-confirm coordination.

Free-function module (no class). Owns the per-doc and per-bundle confirmation
transactions, the orphaned-draft sweep, the bundle suggestion lookup, the
navigation helper for the post-confirm review loop, and the cross-cutting
reset_and_reenrich helper used whenever docs transition between cases (so
the AI enrichment stage re-runs with the new case context).

reset_and_reenrich is public despite being primarily a triage concern because
case_service and the case-confirm/reject endpoints also need it on
case-transition events.
"""

from datetime import datetime

from sqlalchemy import func, or_
from sqlalchemy.orm import Session

from app.models.database import (
    Case,
    Document,
    IngestBatch,
)
from app.models.enums import IngestBatchStatus
from app.repositories.document import DocumentRepository
from app.repositories.ingest_batch import IngestBatchRepository
from app.services.pipeline_status import stages_dict


def _sanitize_case_title(
    title: str | None, case_id: str, bundle_subject: str | None
) -> str | None:
    """Return a display-worthy case title, or None when the raw title is useless.

    Discards titles that are identical to the case_id (AI echo-back) and
    re-derives from the bundle subject via the existing helper, falling back to
    None so the modal field is left blank for the user to fill in.
    """
    from app.services.case_service import _derive_case_title_from_subject

    if title and title.strip() != case_id:
        return title
    derived = _derive_case_title_from_subject(bundle_subject, case_id)
    return derived or None


def reset_and_reenrich(db: Session, docs: list) -> None:
    """Reset ENRICH (and its downstream) to pending, then dispatch enrichment.

    Called whenever docs transition from _TRIAGE to a real case so that
    relationship/claims/entities stages run with the correct case context.
    Only processes docs whose METADATA completed successfully (failed metadata
    means no enrichment output would be meaningful).
    """
    from app.models.enums import PipelineStage
    from app.services.pipeline_status import reset_stage
    from app.tasks.dispatch import dispatch_task
    from app.tasks.enrich_document import enrich_document_task

    for doc in docs:
        metadata_status = stages_dict(doc).get("metadata", {}).get("status")
        if metadata_status != "completed":
            continue
        reset_stage(doc.id, PipelineStage.ENRICH, db)
        dispatch_task(enrich_document_task, doc.id)


def find_next_review_doc(db: Session, after_doc_id: int) -> Document | None:
    """Find the next triage doc needing review after the given one.

    Sibling-first: prefer another doc in the same bundle. Otherwise, the
    first doc in the next bundle. Returns None when the queue is clear.
    """
    from app.services.triage_bundles import get_triage_bundles

    doc_repo = DocumentRepository(db)
    current = doc_repo.get(after_doc_id)
    if not current:
        return None

    if current.ingest_batch_id:
        sibling = (
            db.query(Document)
            .filter(
                Document.ingest_batch_id == current.ingest_batch_id,
                Document.id != after_doc_id,
                or_(Document.case_id == "_TRIAGE", Document.needs_review),
            )
            .order_by(Document.ingest_date.asc())
            .first()
        )
        if sibling:
            return sibling

    bundles = get_triage_bundles(db)
    seen_current_bundle = False
    for bundle in bundles:
        if any(d.id == after_doc_id for d in bundle.documents):
            seen_current_bundle = True
            continue
        if seen_current_bundle:
            for d in bundle.documents:
                if d.needs_review or d.case_id == "_TRIAGE":
                    return d

    # Fallback: any needs_review doc anywhere (if sort changed under us).
    for bundle in bundles:
        for d in bundle.documents:
            if d.id != after_doc_id and (d.needs_review or d.case_id == "_TRIAGE"):
                return d
    return None


def confirm_document(
    db: Session,
    doc_id: int,
    *,
    title: str | None = None,
    case_id: str | None = None,
    originator_type=None,
    sender: str | None = None,
    internal_id: str | None = None,
    issued_date: datetime | None = None,
    received_date: datetime | None = None,
    significance_tier=None,
    document_type=None,
    finalize: bool = False,
) -> Document | None:
    """Apply metadata patch; optionally remove from triage."""
    doc_repo = DocumentRepository(db)
    doc = doc_repo.get(doc_id)
    if not doc:
        return None

    from app.services.ingestion.service import compute_review_reasons
    from app.services.pipeline_status import retry_on_db_locked

    # The mutations live *inside* the retried closure, not just db.commit():
    # retry_on_db_locked's db.rollback() (on a lock-contention retry) expires
    # every attribute this session touched, discarding these not-yet-flushed
    # assignments. Retrying a bare db.commit() after that rollback is a
    # silent no-op — it "succeeds" (200) without ever writing case_id, which
    # is worse than the original Issue #97 500 (data loss instead of a
    # visible error). Redoing the assignments on each attempt keeps every
    # retry idempotent and correct. A still-failing OperationalError after
    # all retries is left to propagate (existing 500 handling) — this
    # cascade isn't optional, unlike the best-effort skip-on-busy pattern
    # used for the idempotent reload latch in bundle_ops.py.
    def _apply_and_commit() -> None:
        if title is not None:
            doc.title = title
        if case_id is not None:
            doc.case_id = case_id
        if originator_type is not None:
            doc.originator_type = originator_type
        if sender is not None:
            doc.sender = sender
        if internal_id is not None:
            doc.internal_id = internal_id or None
        if issued_date is not None:
            doc.issued_date = issued_date
        if received_date is not None:
            doc.received_date = received_date
        if significance_tier is not None:
            doc.significance_tier = significance_tier
        if document_type is not None:
            doc.document_type = document_type

        if finalize:
            conf = dict(doc.extraction_confidence or {})
            field_map = {
                "originator": originator_type,
                "sender": sender,
                "issued_date": issued_date,
                "significance_tier": significance_tier,
                "document_type": document_type,
            }
            for key, val in field_map.items():
                if val is not None:
                    conf[key] = "user_set"
            doc.extraction_confidence = conf

        reasons = compute_review_reasons(doc, confirmed=finalize)
        doc.review_reasons = reasons
        actionable = [r for r in reasons if r != "missing_parent"]
        doc.needs_review = bool(actionable)

        db.commit()

    retry_on_db_locked(_apply_and_commit, db)
    cleanup_orphaned_drafts(db)
    db.refresh(doc)
    return doc


def confirm_bundle(
    db: Session,
    batch_id: int,
    case_id: str,
    proceeding_id: int | None = None,
    finalize: bool = False,
) -> IngestBatch | None:
    """Cascade case/proceeding assignment to every doc in the bundle.

    finalize=True marks the batch COMPLETED unconditionally (used by the
    explicit "Confirm bundle" action). finalize=False (the default, used by
    "Assign case") never touches batch status — the bundle stays in triage
    for further per-doc review.
    """
    from app.models.database import ActionItem, Proceeding
    from app.services.ingestion.service import compute_review_reasons
    from app.services.pipeline_status import retry_on_db_locked

    batch_repo = IngestBatchRepository(db)
    batch = batch_repo.get(batch_id)
    if not batch:
        return None

    docs = db.query(Document).filter(Document.ingest_batch_id == batch_id).all()
    case = db.query(Case).filter(Case.id == case_id).first()
    proc = (
        db.query(Proceeding).filter(Proceeding.id == proceeding_id).first()
        if proceeding_id is not None
        else None
    )
    doc_ids = [doc.id for doc in docs]
    orphaned = (
        db.query(ActionItem)
        .filter(
            ActionItem.source_document_id.in_(doc_ids),
            ActionItem.case_id == "_TRIAGE",
        )
        .all()
        if doc_ids
        else []
    )

    # See confirm_document's matching comment: the mutations below live
    # inside the retried closure, not just db.commit() — retry_on_db_locked's
    # db.rollback() (on a lock-contention retry) expires every attribute
    # these objects carry, so a bare-commit retry would silently no-op
    # instead of re-applying the cascade. Redoing the assignments on each
    # attempt keeps every retry idempotent and correct.
    def _apply_and_commit() -> None:
        if case and case.is_draft:
            case.is_draft = False
        if proc and proc.is_draft:
            proc.is_draft = False

        for doc in docs:
            doc.case_id = case_id
            if proceeding_id is not None:
                doc.proceeding_id = proceeding_id
            reasons = compute_review_reasons(doc, confirmed=finalize)
            doc.review_reasons = reasons
            actionable = [r for r in reasons if r != "missing_parent"]
            doc.needs_review = bool(actionable)

        # Cascade case/proceeding to ActionItems still parked under _TRIAGE.
        for item in orphaned:
            item.case_id = case_id
            if proceeding_id is not None and item.proceeding_id is None:
                item.proceeding_id = proceeding_id

        batch.case_id = case_id
        if proceeding_id is not None:
            batch.proceeding_id = proceeding_id
        if finalize:
            batch.status = IngestBatchStatus.COMPLETED

        db.commit()

    retry_on_db_locked(_apply_and_commit, db)
    cleanup_orphaned_drafts(db)
    db.refresh(batch)
    return batch


def cleanup_orphaned_drafts(db: Session) -> int:
    """Delete draft Case rows whose last document has moved away.

    Drafts are created at the METADATA pipeline stage when an AI-extracted
    internal_id can't be matched to an existing case. If the user later
    assigns the bundle elsewhere, the draft is left orphaned. Cascades
    through to any proceedings the AI created alongside the draft.

    Returns the number of drafts deleted. Commits its own deletes.
    """
    orphaned = (
        db.query(Case)
        .outerjoin(Document, Document.case_id == Case.id)
        .filter(Case.is_draft.is_(True))
        .group_by(Case.id)
        .having(func.count(Document.id) == 0)
        .all()
    )
    for case in orphaned:
        db.delete(case)
    if orphaned:
        db.commit()
    return len(orphaned)


def get_bundle_suggestion(
    db: Session, batch_id: int | None = None, doc_id: int | None = None
) -> tuple[str | None, int | None]:
    """Return (suggested_case_id, suggested_proceeding_id) for a bundle.

    Used by batch confirm to obtain per-bundle suggestions without rebuilding
    the full triage feed. Returns (None, None) when no suggestion exists.
    """
    if batch_id:
        batch = db.get(IngestBatch, batch_id)
        if not batch:
            return None, None
        case_id = (
            batch.case_id if batch.case_id and batch.case_id != "_TRIAGE" else None
        )
        if not case_id:
            doc = (
                db.query(Document)
                .filter(
                    Document.ingest_batch_id == batch_id,
                    Document.case_id.isnot(None),
                    Document.case_id != "_TRIAGE",
                )
                .first()
            )
            case_id = doc.case_id if doc else None
        proceeding_id = batch.proceeding_id if batch.proceeding_id else None
        return case_id, proceeding_id
    elif doc_id:
        doc = db.get(Document, doc_id)
        if not doc:
            return None, None
        case_id = doc.case_id if doc.case_id and doc.case_id != "_TRIAGE" else None
        proceeding_id = doc.proceeding_id if doc.proceeding_id else None
        return case_id, proceeding_id
    return None, None
