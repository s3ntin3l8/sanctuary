"""4b — Per-document relationship detection against prior docs in the same proceeding."""

import logging
from datetime import datetime

from sqlalchemy.orm import Session, defer

from app.config import SessionLocal
from app.models.database import Document, DocumentRelationship
from app.models.enums import RelationshipConfidence, RelationshipType, SignificanceTier
from app.services.ai_config import get_effective_config
from app.services.intelligence._ai_call import call_json_ai
from app.services.intelligence.ai_options import STAGE_OPTIONS
from app.services.intelligence.prompts import RELATIONSHIP_DETECTOR_SYSTEM

logger = logging.getLogger(__name__)

CANDIDATE_TIERS = {SignificanceTier.CRITICAL, SignificanceTier.SIGNIFICANT}
VALID_RELATIONSHIP_TYPES = {e.value for e in RelationshipType}
MAX_CANDIDATES = 15


def _get_prior_docs(doc: Document, db: Session) -> list[Document]:
    """Return up to MAX_CANDIDATES prior docs in the same proceeding."""
    if not doc.proceeding_id:
        return []

    return (
        db.query(Document)
        .options(
            defer(Document.content),
            defer(Document.cost_delta),
        )
        .filter(
            Document.proceeding_id == doc.proceeding_id,
            Document.id != doc.id,
            Document.significance_tier.in_(list(CANDIDATE_TIERS)),
        )
        .order_by(Document.issued_date.desc().nullslast())
        .limit(MAX_CANDIDATES)
        .all()
    )


def _build_candidate_summary(candidate: Document) -> str:
    first_passage = ""
    if candidate.key_passages and isinstance(candidate.key_passages, list):
        first_passage = candidate.key_passages[0].get("text", "")[:200]

    mgmt = candidate.ai_summary or {}
    sig = mgmt.get("legal_significance", "")[:150]

    return (
        f"ID={candidate.id} | {candidate.title} | "
        f"Date={candidate.issued_date.date() if candidate.issued_date else 'unknown'} | "
        f"Author={candidate.attributed_originator or candidate.sender or 'unknown'} | "
        f"Summary={sig} | Key passage: {first_passage}"
    )


def _call_relationship_detector_sync(
    doc: Document,
    candidates: list[Document],
    model: str = "",
    db=None,
) -> dict:
    mgmt = doc.ai_summary or {}
    first_passage = ""
    if doc.key_passages and isinstance(doc.key_passages, list):
        first_passage = doc.key_passages[0].get("text", "")[:200]

    candidate_text = "\n".join(
        f"{i + 1}. {_build_candidate_summary(c)}" for i, c in enumerate(candidates)
    )
    prompt = (
        f"NEW DOCUMENT:\n"
        f"Title: {doc.title}\n"
        f"Summary: {mgmt.get('legal_significance', '')}\n"
        f"Key passage: {first_passage}\n\n"
        f"CANDIDATE PRIOR DOCUMENTS (use only these IDs):\n{candidate_text}"
    )

    return call_json_ai(
        system_prompt=RELATIONSHIP_DETECTOR_SYSTEM,
        user_prompt=prompt,
        options=STAGE_OPTIONS["relationships"],
        debug_label=f"doc_{doc.id}_relationships",
        model=model or None,
        db=db,
        ingest_batch_id=doc.ingest_batch_id,
    )


def detect(doc_id: int) -> str | None:
    """Detect relationships from doc_id to prior documents in the same proceeding.

    Returns a non-empty skip reason if the stage was intentionally skipped,
    or None if it ran (successfully or with a handled exception).
    """
    db: Session = SessionLocal()
    try:
        cfg = get_effective_config(db)
        doc = db.query(Document).filter(Document.id == doc_id).first()
        if not doc:
            logger.warning(f"Doc {doc_id} not found for relationship detection")
            return "document not found"

        if doc.significance_tier not in CANDIDATE_TIERS:
            reason = f"significance_tier={doc.significance_tier} not in candidate tiers"
            logger.info(f"Doc {doc_id}: {reason}, skipping relationship detection")
            return reason

        candidates = _get_prior_docs(doc, db)
        if not candidates:
            reason = f"no prior candidates in proceeding {doc.proceeding_id}"
            logger.info(f"Doc {doc_id}: {reason}")
            return reason

        valid_candidate_ids = {c.id for c in candidates}

        result = _call_relationship_detector_sync(
            doc, candidates, model=cfg.summary_model, db=db
        )
        relationships = result.get("relationships") or []

        for rel in relationships:
            to_id = rel.get("to_document_id")
            rel_type_raw = (rel.get("relationship_type") or "").lower()

            if to_id not in valid_candidate_ids:
                logger.info(
                    f"Doc {doc_id}: relationship to ID {to_id} not in candidates, dropping"
                )
                continue

            if rel_type_raw not in VALID_RELATIONSHIP_TYPES:
                logger.info(
                    f"Doc {doc_id}: invalid relationship_type '{rel_type_raw}', dropping"
                )
                continue

            existing = (
                db.query(DocumentRelationship)
                .filter(
                    DocumentRelationship.from_document_id == doc_id,
                    DocumentRelationship.to_document_id == to_id,
                    DocumentRelationship.relationship_type
                    == RelationshipType(rel_type_raw),
                )
                .first()
            )
            if existing:
                continue

            notes = f"AI confidence: {rel.get('confidence', 'unknown')}. {rel.get('notes', '')}"
            db.add(
                DocumentRelationship(
                    from_document_id=doc_id,
                    to_document_id=to_id,
                    relationship_type=RelationshipType(rel_type_raw),
                    confidence=RelationshipConfidence.AI_DETECTED,
                    notes=notes[:500],
                    ingest_date=datetime.now(),
                )
            )

        db.commit()
        logger.info(
            f"Doc {doc_id}: relationship detection complete, {len(relationships)} proposed"
        )
    finally:
        db.rollback()
        db.close()
    return None
