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

import logging
from datetime import datetime

from sqlalchemy import func
from sqlalchemy.orm import Session

from app.core.timezone import now_utc
from app.models.database import (
    Case,
    Document,
    IngestBatch,
)
from app.models.enums import DocumentStatus, IngestBatchStatus
from app.repositories.document import DocumentRepository
from app.repositories.ingest_batch import IngestBatchRepository
from app.services.pipeline_status import stages_dict

logger = logging.getLogger(__name__)


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

    Skips docs whose ENRICH is already RUNNING or RETRYING — the check is part
    of the reset's UPDATE (see ``reset_stage``), so resetting it out from under
    an in-flight run, and the double dispatch that would follow, cannot happen.
    One known gap remains: if the in-flight run reads doc.case_id before this
    case transition commits, it produces output against the stale
    (pre-transition) case context and nothing here re-triggers a fresh enrich
    afterward.
    """
    from app.models.enums import PipelineStage, StageStatus
    from app.services.pipeline_status import claim_stage_for_dispatch, reset_stage
    from app.tasks.dispatch import dispatch_task
    from app.tasks.enrich_document import enrich_document_task

    for doc in docs:
        stages = stages_dict(doc)
        metadata_status = stages.get("metadata", {}).get("status")
        if metadata_status != "completed":
            continue
        enrich_status = stages.get("enrich", {}).get("status")
        if enrich_status in (StageStatus.RUNNING.value, StageStatus.RETRYING.value):
            logger.info(
                "reset_and_reenrich: doc %d ENRICH already %s — skipping",
                doc.id,
                enrich_status,
            )
            continue
        # The guarded reset is atomic with the in-flight check, so a dispatcher
        # that claimed ENRICH after the status read above is never clobbered.
        if not reset_stage(doc.id, PipelineStage.ENRICH, db):
            continue
        # ENRICH is a caller-claimed stage (mark_starts unconditionally) —
        # claim it before dispatch for the same double-dispatch protection
        # every other cascade dispatcher uses.
        if claim_stage_for_dispatch(doc.id, PipelineStage.ENRICH, db):
            dispatch_task(enrich_document_task, doc.id)


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

    from app.services.ingestion.service import apply_review_reasons
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

        # Whatever the user supplied is now verified, whether or not this call
        # also files the document: it clears "low confidence" and tells later
        # AI runs not to overwrite it.
        field_map = {
            "title": title,
            "originator_type": originator_type,
            "sender": sender,
            "internal_id": internal_id,
            "issued_date": issued_date,
            "significance_tier": significance_tier,
            "document_type": document_type,
        }
        conf = dict(doc.extraction_confidence or {})
        for key, val in field_map.items():
            if val not in (None, ""):
                conf[key] = "user_set"
        doc.extraction_confidence = conf

        if finalize and doc.confirmed_at is None:
            doc.confirmed_at = now_utc()
        apply_review_reasons(doc)

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
    from app.services.ingestion.service import apply_review_reasons
    from app.services.pipeline_status import retry_on_db_locked

    batch_repo = IngestBatchRepository(db)
    batch = batch_repo.get(batch_id)
    if not batch:
        return None

    docs = (
        db.query(Document)
        .filter(
            Document.ingest_batch_id == batch_id,
            Document.status != DocumentStatus.DISMISSED,
        )
        .all()
    )
    case = db.query(Case).filter(Case.id == case_id).first()
    proc = (
        db.query(Proceeding).filter(Proceeding.id == proceeding_id).first()
        if proceeding_id is not None
        else None
    )
    if proc is not None and proc.case_id != case_id:
        # Defense-in-depth: route-level callers already validate this, but a
        # proceeding_id from a different case must never get cascaded onto
        # every doc in this bundle even if a caller forgets to check.
        proc = None
        proceeding_id = None
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
            if finalize and doc.confirmed_at is None:
                doc.confirmed_at = now_utc()
            apply_review_reasons(doc)

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
