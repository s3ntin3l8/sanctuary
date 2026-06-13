"""4b — Per-document relationship detection against prior docs in the same proceeding."""

import logging
from datetime import UTC, datetime

from sqlalchemy.orm import Session, defer

from app.config import SessionLocal
from app.models.database import Document, DocumentRelationship, Proceeding
from app.models.enums import RelationshipConfidence, RelationshipType, SignificanceTier
from app.services.ai_config import get_chat_config
from app.services.embeddings import nearest_document_ids
from app.services.intelligence._ai_call import call_json_ai
from app.services.intelligence.ai_options import STAGE_OPTIONS
from app.services.intelligence.prompts import RELATIONSHIP_DETECTOR_SYSTEM
from app.services.intelligence.schemas import RelationshipDetection

logger = logging.getLogger(__name__)

CANDIDATE_TIERS = {SignificanceTier.CRITICAL, SignificanceTier.SIGNIFICANT}
VALID_RELATIONSHIP_TYPES = {e.value for e in RelationshipType}
MAX_CANDIDATES = 20
# Of MAX_CANDIDATES, reserve this many slots for semantic neighbours the recency
# window missed; the rest go to the most recent docs. Reserving slots is what
# makes the blend work — once a case has more prior docs than MAX_CANDIDATES the
# recency window is always full, so without a reservation the semantic half could
# never contribute.
_SEMANTIC_SLOTS = 6
# vec0 KNN is global; over-fetch then prune to this case/tier/prior-id.
_KNN_OVERFETCH = 6


def _get_first_passage(doc: Document) -> str:
    """Safely extract and truncate the first key passage."""
    if (
        doc.key_passages
        and isinstance(doc.key_passages, list)
        and len(doc.key_passages) > 0
    ):
        return doc.key_passages[0].get("text", "")[:200]
    return ""


def _build_query_text(doc: Document) -> str:
    """Compact query string for semantic candidate lookup — the same fields the
    detector prompt is built from (title + legal significance + first passage)."""
    mgmt = doc.ai_summary or {}
    parts = [
        doc.title or "",
        mgmt.get("legal_significance", "") or "",
        _get_first_passage(doc),
    ]
    return "\n".join(p for p in parts if p).strip()


def _get_prior_docs(doc: Document, db: Session) -> list[Document]:
    """Return up to MAX_CANDIDATES prior docs in the same case, combining a
    recency window with semantic nearest-neighbours.

    Recency (id DESC) is the high-precision half: direct replies are almost
    always to recent docs, and it always catches same-batch siblings whose
    embeddings may not be indexed yet. Semantic KNN is the high-recall half: it
    surfaces relevant *older* docs that fall outside the recency window. The
    union is strictly >= the recency-only behaviour, so it cannot regress.
    Recency candidates lead; semantic-only candidates fill the remaining slots.
    """
    case_id = doc.case_id
    if not case_id and doc.proceeding_id:
        # Fallback if case_id is missing but proceeding_id is present
        proceeding = (
            db.query(Proceeding).filter(Proceeding.id == doc.proceeding_id).first()
        )
        if proceeding:
            case_id = proceeding.case_id

    if not case_id:
        return []

    def _scoped():
        return (
            db.query(Document)
            .options(defer(Document.content))
            .filter(
                Document.case_id == case_id,
                Document.id < doc.id,  # Strictly prior documents
                Document.significance_tier.in_(list(CANDIDATE_TIERS)),
            )
        )

    recent = _scoped().order_by(Document.id.desc()).limit(MAX_CANDIDATES).all()
    recent_ids = {c.id for c in recent}

    # Semantic neighbours the recency window did NOT already include.
    knn_ids = nearest_document_ids(
        _build_query_text(doc), db, k=MAX_CANDIDATES * _KNN_OVERFETCH
    )
    extra_ids = [i for i in knn_ids if i not in recent_ids]
    semantic: list[Document] = []
    if extra_ids:
        # Re-apply case/tier/prior filters so global KNN hits from other cases or
        # wrong tiers are pruned, then restore KNN distance order.
        docs = _scoped().filter(Document.id.in_(extra_ids)).all()
        rank = {doc_id: pos for pos, doc_id in enumerate(extra_ids)}
        docs.sort(key=lambda d: rank.get(d.id, len(extra_ids)))
        semantic = docs

    if not semantic:
        # Recency-only — also the embed-failure / cold-index fallback. Identical
        # to the pre-A1 behaviour, so this path cannot regress.
        return recent

    # Blend: give the closest semantic neighbours up to _SEMANTIC_SLOTS, fill the
    # remainder with the most recent. Recency leads (high precision for replies),
    # semantic-only candidates trail (recall for older referenced docs).
    sem_take = semantic[:_SEMANTIC_SLOTS]
    rec_take = recent[: MAX_CANDIDATES - len(sem_take)]
    return (rec_take + sem_take)[:MAX_CANDIDATES]


def _build_candidate_summary(candidate: Document) -> str:
    from app.services.intelligence.prompts import sanitize_oneline

    first_passage = _get_first_passage(candidate)

    mgmt = candidate.ai_summary or {}
    sig = mgmt.get("legal_significance", "")[:150]

    return (
        f"ID={candidate.id} | "
        f"{sanitize_oneline(candidate.title, 200)} | "
        f"Date={candidate.issued_date.date() if candidate.issued_date else 'unknown'} | "
        f"Author={sanitize_oneline(candidate.attributed_originator or candidate.sender, 100) or 'unknown'} | "
        f"Summary={sanitize_oneline(sig, 200)} | "
        f"Key passage: {sanitize_oneline(first_passage, 200)}"
    )


def _call_relationship_detector_sync(
    doc: Document,
    candidates: list[Document],
    model: str = "",
) -> dict:
    """AI call only — no DB session held."""
    mgmt = doc.ai_summary or {}
    first_passage = _get_first_passage(doc)

    from app.services.intelligence.prompts import sanitize_oneline

    candidate_text = "\n".join(
        f"{i + 1}. {_build_candidate_summary(c)}" for i, c in enumerate(candidates)
    )
    prompt = (
        f"NEW DOCUMENT:\n"
        f"Title: {sanitize_oneline(doc.title, 200)}\n"
        f"Summary: {sanitize_oneline(mgmt.get('legal_significance', ''), 400)}\n"
        f"Key passage: {sanitize_oneline(first_passage, 400)}\n\n"
        f"CANDIDATE PRIOR DOCUMENTS (use only these IDs):\n{candidate_text}"
    )

    result = call_json_ai(
        system_prompt=RELATIONSHIP_DETECTOR_SYSTEM,
        user_prompt=prompt,
        options=STAGE_OPTIONS["relationships"],
        debug_label=f"doc_{doc.id}_relationships",
        schema=RelationshipDetection,
        model=model or None,
        ingest_batch_id=doc.ingest_batch_id,
        case_id=doc.case_id,
        two_pass=True,
        # Per-doc stage: suppress the case-narrative preamble (Issue #5).
        include_user_context=False,
    )
    return result.model_dump()


def detect(doc_id: int) -> str | None:
    """Detect relationships from doc_id to prior documents in the same case.

    Returns a non-empty skip reason if the stage was intentionally skipped,
    None if it ran successfully, or an error string if an exception occurred.
    """
    # Phase 1: read
    db: Session = SessionLocal()
    try:
        cfg = get_chat_config(db)
        from app.services.ai_provider import chat_provider

        chat_provider.reload_from_db(db)
        doc = (
            db.query(Document)
            .options(defer(Document.content))
            .filter(Document.id == doc_id)
            .first()
        )
        if not doc:
            logger.warning(f"Doc {doc_id} not found for relationship detection")
            return "document not found"

        if doc.significance_tier not in CANDIDATE_TIERS:
            reason = f"significance_tier={doc.significance_tier} not in candidate tiers"
            logger.info(f"Doc {doc_id}: {reason}, skipping relationship detection")
            return reason

        candidates = _get_prior_docs(doc, db)
        if not candidates:
            reason = "no prior candidates in case"
            logger.info(f"Doc {doc_id}: {reason}")
            return reason

        valid_candidate_ids = {c.id for c in candidates}
        existing_rels = (
            db.query(
                DocumentRelationship.to_document_id,
                DocumentRelationship.relationship_type,
            )
            .filter(DocumentRelationship.from_document_id == doc_id)
            .all()
        )
        existing_set = {(r.to_document_id, r.relationship_type) for r in existing_rels}
        candidate_date_map = {c.id: c.issued_date for c in candidates}
        model = cfg.summary_model
        # doc and candidates remain accessible after session closes
    finally:
        db.close()

    # Phase 2: AI call — no DB session held
    try:
        result = _call_relationship_detector_sync(doc, candidates, model=model)
    except Exception as e:
        logger.exception(f"Doc {doc_id}: failed relationship detection: {e}")
        return f"error: {str(e)}"

    # Phase 3: write
    relationships = result.get("relationships") or []
    db = SessionLocal()
    try:
        new_count = 0
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

            rel_type_enum = RelationshipType(rel_type_raw)
            if (to_id, rel_type_enum) in existing_set:
                continue

            if rel_type_enum in (
                RelationshipType.SUPERSEDES,
                RelationshipType.REPLIES_TO,
            ):
                target_date = candidate_date_map.get(to_id)
                if doc.issued_date and target_date and doc.issued_date < target_date:
                    logger.info(
                        "Doc %d: dropping %s→%d — new doc (%s) predates target (%s)",
                        doc_id,
                        rel_type_raw,
                        to_id,
                        doc.issued_date.date(),
                        target_date.date(),
                    )
                    continue

            notes = f"AI confidence: {rel.get('confidence', 'unknown')}. {rel.get('notes', '')}"
            db.add(
                DocumentRelationship(
                    from_document_id=doc_id,
                    to_document_id=to_id,
                    relationship_type=rel_type_enum,
                    confidence=RelationshipConfidence.AI_DETECTED,
                    notes=notes[:500],
                    ingest_date=datetime.now(UTC),
                )
            )
            new_count += 1

        db.commit()
        logger.info(
            f"Doc {doc_id}: relationship detection complete, {new_count} new links created"
        )
        return None
    except Exception as e:
        db.rollback()
        logger.exception(f"Doc {doc_id}: failed relationship detection: {e}")
        return f"error: {str(e)}"
    finally:
        db.close()
