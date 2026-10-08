"""4b — Per-document relationship detection against prior docs in the same proceeding."""

import logging
from typing import NamedTuple

from sqlalchemy import and_, case, literal, or_, tuple_
from sqlalchemy.orm import Session, defer

from app.models.database import Document, DocumentRelationship, Proceeding
from app.models.enums import RelationshipConfidence, RelationshipType, SignificanceTier
from app.repositories.document_relationship import insert_edge_if_absent
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
# pgvector KNN is global; over-fetch then prune to this case/tier/prior-id.
_KNN_OVERFETCH = 6


class _Candidate(NamedTuple):
    """A prior doc plus which candidate pool(s) surfaced it: ``"thread"``
    (same-thread signals / recency) and/or ``"topic"`` (semantic KNN)."""

    doc: Document
    via: frozenset[str]


def thread_slots() -> int:
    """Slots the threading pool gets; the rest are reserved for topical hits."""
    return MAX_CANDIDATES - _SEMANTIC_SLOTS


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


def prior_filter(doc: Document):
    """SQL predicate: documents that come *before* ``doc`` in the case.

    "Before" is by document date (``issued_date``, id as tie-break), not by
    arrival order, so an old letter that was scanned late still sees only older
    docs and the newer docs see it. A dated doc's priors are the dated docs
    with an earlier ``(issued_date, id)``, plus undated docs that arrived
    earlier; an undated doc falls back to arrival order (``id``).
    """
    if doc.issued_date is None:
        return Document.id < doc.id
    return or_(
        and_(
            Document.issued_date.is_not(None),
            tuple_(Document.issued_date, Document.id)
            < tuple_(literal(doc.issued_date), literal(doc.id)),
        ),
        and_(Document.issued_date.is_(None), Document.id < doc.id),
    )


def successor_filter(doc: Document):
    """SQL predicate: documents for which ``doc`` is a prior (the inverse of
    ``prior_filter``)."""
    if doc.issued_date is None:
        # An undated doc is a prior of a doc exactly when it arrived earlier.
        return Document.id > doc.id
    return or_(
        and_(
            Document.issued_date.is_not(None),
            tuple_(Document.issued_date, Document.id)
            > tuple_(literal(doc.issued_date), literal(doc.id)),
        ),
        and_(Document.issued_date.is_(None), Document.id > doc.id),
    )


def _get_prior_docs(doc: Document, db: Session) -> list[_Candidate]:
    """Return up to MAX_CANDIDATES prior docs in the same case, each tagged with
    the candidate pool that surfaced it.

    The relation types want different signals, so there are two pools:

    * **thread** (``replies_to`` / ``supersedes``): docs sharing the new doc's
      Aktenzeichen / file reference / proceeding first, then the closest earlier
      document date. High precision, and it always catches same-batch siblings
      whose embeddings may not be indexed yet. Embedding similarity is a weak
      proxy here — a terse reply is topically thin.
    * **topic** (``references`` / ``attaches_as_proof``): pgvector nearest
      neighbours, which surface relevant *older* docs outside the threading pool.

    The union is strictly >= the recency-only behaviour. A doc surfaced by both
    pools carries both tags. Thread candidates lead; topical-only candidates
    fill the reserved ``_SEMANTIC_SLOTS``.
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
        q = (
            db.query(Document)
            .options(defer(Document.content))
            .filter(
                Document.case_id == case_id,
                prior_filter(doc),  # Strictly prior documents (by date)
                Document.significance_tier.in_(list(CANDIDATE_TIERS)),
            )
        )
        if case_id == "_TRIAGE":
            # _TRIAGE is one bucket shared by every user: candidates (and the
            # edges built from them) must stay within the uploader's own docs.
            # Any other case_id that is shared across users in future needs the
            # same treatment, or the cross-owner leak silently comes back.
            q = q.filter(Document.owner_id == doc.owner_id)
        return q

    thread_order = [Document.issued_date.desc().nulls_last(), Document.id.desc()]
    signals = []
    if doc.az_court:
        signals.append(Document.az_court == doc.az_court)
    if doc.internal_id:
        signals.append(Document.internal_id == doc.internal_id)
    if doc.proceeding_id is not None:
        signals.append(Document.proceeding_id == doc.proceeding_id)
    if signals:
        thread_order.insert(0, case((or_(*signals), 0), else_=1))
    threading = _scoped().order_by(*thread_order).limit(MAX_CANDIDATES).all()
    thread_ids = {c.id for c in threading}

    # Semantic neighbours, whether or not the threading pool already has them.
    knn_ids = nearest_document_ids(
        _build_query_text(doc), db, k=MAX_CANDIDATES * _KNN_OVERFETCH
    )
    topical: list[Document] = []
    if knn_ids:
        # Re-apply case/tier/prior filters so global KNN hits from other cases or
        # wrong tiers are pruned, then restore KNN distance order.
        docs = _scoped().filter(Document.id.in_(knn_ids)).all()
        rank = {doc_id: pos for pos, doc_id in enumerate(knn_ids)}
        docs.sort(key=lambda d: rank.get(d.id, len(knn_ids)))
        topical = docs

    # Blend: the closest topical-only neighbours take up to _SEMANTIC_SLOTS, the
    # threading pool fills the remainder. With no topical-only hits (embed
    # failure, cold index) this is the threading pool alone, so it cannot regress.
    topic_ids = {d.id for d in topical}
    topic_only = [d for d in topical if d.id not in thread_ids]
    sem_take = topic_only[:_SEMANTIC_SLOTS]
    rec_take = threading[: MAX_CANDIDATES - len(sem_take)]
    out = [
        _Candidate(
            d, frozenset({"thread", "topic"} if d.id in topic_ids else {"thread"})
        )
        for d in rec_take
    ]
    out += [_Candidate(d, frozenset({"topic"})) for d in sem_take]
    return out[:MAX_CANDIDATES]


def _build_candidate_summary(cand: _Candidate) -> str:
    from app.services.intelligence.prompts import sanitize_oneline

    candidate = cand.doc

    first_passage = _get_first_passage(candidate)

    mgmt = candidate.ai_summary or {}
    sig = mgmt.get("legal_significance", "")[:150]

    return (
        f"ID={candidate.id} | "
        f"{sanitize_oneline(candidate.title, 200)} | "
        f"Date={candidate.issued_date.date() if candidate.issued_date else 'unknown'} | "
        f"AZ={sanitize_oneline(candidate.az_court, 60) or '-'} | "
        f"Via={'+'.join(sorted(cand.via))} | "
        f"Author={sanitize_oneline(candidate.attributed_originator or candidate.sender, 100) or 'unknown'} | "
        f"Summary={sanitize_oneline(sig, 200)} | "
        f"Key passage: {sanitize_oneline(first_passage, 200)}"
    )


def _call_relationship_detector_sync(
    doc: Document,
    candidates: list[_Candidate],
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
    or None if it ran successfully. Raises on AI-call or write-phase failure
    — the caller (detect_relationships_task) is responsible for retry/
    failure handling, matching the pattern used by enrich_document_task and
    metadata_task. This used to catch every exception here and return it as
    an "error: ..." string, which detect_relationships_task's own retry
    logic never saw (it only fires on a raised exception) and which got
    recorded as RELATIONSHIPS=SKIPPED — a transient timeout was silently
    treated the same as an intentional skip, with no retry.
    """
    from app.config import SessionLocal

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

        valid_candidate_ids = {c.doc.id for c in candidates}
        existing_rels = (
            db.query(
                DocumentRelationship.to_document_id,
                DocumentRelationship.relationship_type,
            )
            .filter(DocumentRelationship.from_document_id == doc_id)
            .all()
        )
        existing_set = {(r.to_document_id, r.relationship_type) for r in existing_rels}
        # Documents that already reply to this one (e.g. an out-of-order email
        # header edge from a lower id). The reverse REPLIES_TO would be a 2-cycle.
        replied_by = {
            r[0]
            for r in db.query(DocumentRelationship.from_document_id).filter(
                DocumentRelationship.to_document_id == doc_id,
                DocumentRelationship.relationship_type == RelationshipType.REPLIES_TO,
            )
        }
        model = cfg.summary_model
        # doc and candidates remain accessible after session closes
    finally:
        db.close()

    # Phase 2: AI call — no DB session held. Let exceptions propagate; the
    # caller decides retry vs. terminal failure.
    result = _call_relationship_detector_sync(doc, candidates, model=model)

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

            if rel_type_enum == RelationshipType.REPLIES_TO and to_id in replied_by:
                logger.info(
                    "Doc %d: dropping replies_to→%d — %d already replies to this doc",
                    doc_id,
                    to_id,
                    to_id,
                )
                continue

            notes = f"AI confidence: {rel.get('confidence', 'unknown')}. {rel.get('notes', '')}"
            if insert_edge_if_absent(
                db,
                from_document_id=doc_id,
                to_document_id=to_id,
                relationship_type=rel_type_enum,
                confidence=RelationshipConfidence.AI_DETECTED,
                notes=notes[:500],
            ):
                new_count += 1

        db.commit()
        logger.info(
            f"Doc {doc_id}: relationship detection complete, {new_count} new links created"
        )
        return None
    except Exception:
        db.rollback()
        raise
    finally:
        db.close()
