"""Backfill for late-arriving documents: re-run relationship detection for the
newer documents of a case that ran before an older document was available.

Detection links a document to its priors (earlier document date). A letter that
is scanned late therefore only ever looks at even-older docs, and the newer docs
that should reference or reply to it already ran without it. After a document's
own detection completes, ``dispatch_backfill`` re-queues a bounded set of those
newer docs. Re-runs only add edges (``insert_edge_if_absent`` dedups and honours
rejected edges), so repeating detection is safe.
"""

import logging

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from app.models.database import Document, DocumentPipelineStage
from app.models.enums import PipelineStage, StageStatus
from app.services.embeddings import nearest_document_ids
from app.services.intelligence.relationship_detector import (
    _KNN_OVERFETCH,
    CANDIDATE_TIERS,
    MAX_CANDIDATES,
    _build_query_text,
    prior_filter,
    successor_filter,
)

logger = logging.getLogger(__name__)

MAX_BACKFILL = 10
_TRIAGE_CASE = "_TRIAGE"


def _case_scope(doc: Document, db: Session):
    """Candidate-tier documents of ``doc``'s case (never called for ``_TRIAGE``)."""
    return db.query(Document).filter(
        Document.case_id == doc.case_id,
        Document.significance_tier.in_(list(CANDIDATE_TIERS)),
    )


def stale_successors(db: Session, doc: Document) -> list[int]:
    """Ids of newer docs that completed detection before ``doc`` was enriched
    and for which ``doc`` would be a relevant candidate. Nearest-dated first,
    at most ``MAX_BACKFILL``."""
    if not doc.case_id or doc.case_id == _TRIAGE_CASE:
        # Triage docs are re-enriched and re-detected when routed to a case.
        return []

    enrich_done = (
        db.query(DocumentPipelineStage.completed_at)
        .filter(
            DocumentPipelineStage.document_id == doc.id,
            DocumentPipelineStage.stage == PipelineStage.ENRICH.value,
        )
        .scalar()
    )
    if enrich_done is None:
        return []

    ran_before = select(DocumentPipelineStage.document_id).where(
        DocumentPipelineStage.stage == PipelineStage.RELATIONSHIPS.value,
        DocumentPipelineStage.status == StageStatus.COMPLETED.value,
        DocumentPipelineStage.completed_at < enrich_done,
    )
    successors = (
        _case_scope(doc, db)
        .filter(
            successor_filter(doc),
            Document.id != doc.id,
            Document.id.in_(ran_before),
        )
        .order_by(Document.issued_date.asc().nulls_last(), Document.id.asc())
        .all()
    )
    if not successors:
        return []

    knn = set(
        nearest_document_ids(
            _build_query_text(doc), db, k=MAX_CANDIDATES * _KNN_OVERFETCH
        )
    )
    picked: list[int] = []
    for succ in successors:
        if succ.id in knn or _in_recency_window(db, doc, succ):
            picked.append(succ.id)
            if len(picked) >= MAX_BACKFILL:
                break
    return picked


def _in_recency_window(db: Session, doc: Document, succ: Document) -> bool:
    """True when fewer than ``MAX_CANDIDATES`` of ``succ``'s priors lie between
    ``doc`` and ``succ`` — i.e. ``doc`` was inside ``succ``'s recency window."""
    between = (
        _case_scope(doc, db)
        .filter(prior_filter(succ), successor_filter(doc))
        .with_entities(func.count(Document.id))
        .scalar()
    )
    return (between or 0) < MAX_CANDIDATES


def dispatch_backfill(doc_id: int) -> int:
    """Re-queue relationship detection for the newer docs ``doc_id`` was missing
    from. Returns how many were dispatched."""
    from app.dependencies import get_db_session
    from app.services.pipeline_status import (
        claim_stage_for_dispatch,
        mark_failed,
        reset_stage,
    )
    from app.tasks.detect_relationships import detect_relationships_task

    db = get_db_session()
    try:
        doc = db.get(Document, doc_id)
        if doc is None:
            return 0
        targets = stale_successors(db, doc)
        dispatched = 0
        for target in targets:
            # reset_stage leaves an in-flight stage alone and reports False.
            if not reset_stage(target, PipelineStage.RELATIONSHIPS, db):
                continue
            if not claim_stage_for_dispatch(target, PipelineStage.RELATIONSHIPS, db):
                continue
            try:
                detect_relationships_task.delay(target, backfill=False)
                dispatched += 1
            except Exception as e:
                logger.error(
                    "Doc #%d: backfill dispatch failed: %s", target, e, exc_info=True
                )
                mark_failed(
                    target,
                    PipelineStage.RELATIONSHIPS,
                    db,
                    error=f"dispatch failed: {e}",
                )
        if dispatched:
            logger.info(
                "Doc #%d: re-queued relationship detection for %d newer doc(s)",
                doc_id,
                dispatched,
            )
        return dispatched
    finally:
        db.close()
