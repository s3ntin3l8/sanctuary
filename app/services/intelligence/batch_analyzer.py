"""4a — Per-batch AI pass: cover-letter detection, originator attribution, action items.

Supports multi-bundle format (new):
- bundles: [{"cover_letter_doc_id": int|null, "enclosed": [...]}]
- Each bundle wires its enclosures to its cover letter

Legacy format (backward compat):
- cover_letter_doc_id, is_cover_letter, enclosed_descriptions
"""

import logging
import re

from sqlalchemy.orm import Session

from app.config import SessionLocal
from app.models.database import Document, IngestBatch
from app.models.enums import (
    DocumentRole,
    OriginatorType,
    parse_originator_type,
)
from app.services.ai_config import get_chat_config
from app.services.ai_summary import get_content_preview
from app.services.intelligence._ai_call import call_json_ai
from app.services.intelligence.ai_options import STAGE_OPTIONS
from app.services.intelligence.prompts import BATCH_ANALYZER_SYSTEM

logger = logging.getLogger(__name__)

COVER_LETTER_KEYWORDS = {
    "begleitschreiben",
    "anschreiben",
    "übersendungsschreiben",
    "deckblatt",
    "cover",
}


def _metadata_outranks_batch(child: Document, batch_originator: str | None) -> bool:
    """True when the metadata stage determined a non-court sender and the batch
    stage is trying to overwrite it with 'court'.

    The metadata stage sees full text; the batch stage sees one cover-letter
    candidate plus sibling titles only. OWN/OPPOSING/THIRD_PARTY from metadata
    outranks a title-only 'court' guess from batch.
    """
    if not batch_originator or batch_originator.lower() != "court":
        return False
    return child.originator_type in (
        OriginatorType.OWN,
        OriginatorType.OPPOSING,
        OriginatorType.THIRD_PARTY,
    )


def _pick_cover_letter_candidate(docs: list[Document]) -> Document | None:
    """Heuristic: pick the most likely cover letter candidate from the batch.
    Only considers documents with actual content.
    """
    healthy_docs = [d for d in docs if d.content and len(d.content.strip()) > 10]

    for doc in healthy_docs:
        lower = (doc.title or "").lower()
        if any(kw in lower for kw in COVER_LETTER_KEYWORDS):
            return doc

    if healthy_docs:
        return min(healthy_docs, key=lambda d: len(d.content or ""))

    return None


def _call_batch_analyzer_sync(
    candidate: Document,
    sibling_titles: list[str],
    batch_id: int,
    model: str = "",
    db=None,
) -> dict:
    """Synchronous AI call for batch analysis."""
    content_preview = get_content_preview(candidate, 60000)
    sibling_list = "\n".join(f"- {t}" for t in sibling_titles)
    prompt = (
        f"Cover letter candidate (doc_id={candidate.id}):\n"
        f"Title: {candidate.title}\n\n"
        f"{content_preview}\n\n"
        f"Other documents in this batch:\n{sibling_list}"
    )

    return call_json_ai(
        system_prompt=BATCH_ANALYZER_SYSTEM,
        user_prompt=prompt,
        options=STAGE_OPTIONS["batch_analysis"],
        debug_label=f"batch_{batch_id}_analyzer",
        model=model or None,
        db=db,
        ingest_batch_id=batch_id,
    )


def _norm_filename(s: str) -> str:
    """Normalize filename for matching."""
    s = re.sub(r"\.[a-zA-Z]{2,5}$", "", s)
    return re.sub(r"[-_.\s]+", " ", s).lower().strip()


def _apply_batch_results(
    batch_id: int,
    docs: list[Document],
    result: dict,
    db: Session,
) -> None:
    """Write batch analyzer results to the DB.

    Supports both multi-bundle format (new) and legacy format (backward compat).
    New format: bundles = [{"cover_letter_doc_id": int|null, "enclosed": [...]}]
    Legacy format: cover_letter_doc_id, is_cover_letter, enclosed_descriptions
    """
    bundles = result.get("bundles")
    detected_actions = result.get("detected_actions") or []

    doc_map = {d.id: d for d in docs}
    claimed_ids: set[int] = set()
    first_cover: Document | None = None

    # Check if we have the new multi-bundle format
    if bundles and isinstance(bundles, list):
        # Process each bundle
        for bundle in bundles:
            cover_id = bundle.get("cover_letter_doc_id")
            enclosed = bundle.get("enclosed") or []

            cover_doc = doc_map.get(cover_id) if cover_id else None
            if cover_doc:
                cover_doc.role = DocumentRole.COVER_LETTER
                has_court = any(e.get("originator_type") == "court" for e in enclosed)
                cover_doc.court_relay = has_court

                # Set attribution from first enclosure
                first_enclosure = next(
                    (
                        e.get("attributed_originator")
                        for e in enclosed
                        if e.get("attributed_originator")
                    ),
                    None,
                )
                cover_doc.attributed_originator = first_enclosure
                if first_cover is None:
                    first_cover = cover_doc

            # Without a cover letter the AI is signaling the doc is standalone,
            # not enclosed under anything. Skip enclosure wiring and let the
            # unclaimed-fallback at the end mark the doc STANDALONE.
            if cover_id is None:
                continue

            # Wire enclosures to this cover letter
            for encl in enclosed:
                matched = encl.get("matched_filename")
                child = None
                if matched:
                    matched_norm = _norm_filename(matched)
                    candidates = [
                        d for d in docs if d.id != cover_id and d.id not in claimed_ids
                    ]
                    child = next(
                        (
                            d
                            for d in candidates
                            if _norm_filename(d.title or "") == matched_norm
                        ),
                        None,
                    )
                    if not child:
                        subs = [
                            d
                            for d in candidates
                            if matched_norm in _norm_filename(d.title or "")
                            or _norm_filename(d.title or "") in matched_norm
                        ]
                        if len(subs) == 1:
                            child = subs[0]
                if child:
                    if _metadata_outranks_batch(child, encl.get("originator_type")):
                        logger.warning(
                            "Batch #%d doc #%d: skipping enclosure assignment — metadata "
                            "classified originator as %s but batch claims 'court'. Trusting metadata.",
                            batch_id,
                            child.id,
                            child.originator_type,
                        )
                        continue
                    claimed_ids.add(child.id)
                    child.role = DocumentRole.ENCLOSURE
                    child.parent_id = cover_id
                    child.originator_type = (
                        parse_originator_type(encl.get("originator_type"))
                        or child.originator_type
                    )
                    # For non-court enclosures, prefer metadata's sender extraction
                    # (full text) over batch's title-only guess. Court relays are
                    # the legitimate "batch knows better" case.
                    batch_originator = encl.get("attributed_originator")
                    is_court = encl.get("originator_type", "").lower() == "court"
                    if is_court or not child.attributed_originator:
                        child.attributed_originator = batch_originator
    else:
        # Legacy format: single cover letter
        cover_letter_doc_id = result.get("cover_letter_doc_id")
        is_cover_letter = result.get("is_cover_letter", False)
        court_relay = result.get("court_relay", False)
        enclosed_descriptions = result.get("enclosed_descriptions") or []

        cover_letter_doc = (
            doc_map.get(cover_letter_doc_id) if cover_letter_doc_id else None
        )
        if cover_letter_doc and is_cover_letter:
            cover_letter_doc.role = DocumentRole.COVER_LETTER
            cover_letter_doc.court_relay = bool(court_relay)
            cover_letter_doc.attributed_originator = next(
                (
                    d.get("attributed_originator")
                    for d in enclosed_descriptions
                    if d.get("attributed_originator")
                ),
                None,
            )
            first_cover = cover_letter_doc

        for encl in enclosed_descriptions:
            matched = encl.get("matched_filename")
            child = None
            if matched:
                matched_norm = _norm_filename(matched)
                candidates = [
                    d
                    for d in docs
                    if d.id != cover_letter_doc_id and d.id not in claimed_ids
                ]
                child = next(
                    (
                        d
                        for d in candidates
                        if _norm_filename(d.title or "") == matched_norm
                    ),
                    None,
                )
                if not child:
                    subs = [
                        d
                        for d in candidates
                        if matched_norm in _norm_filename(d.title or "")
                        or _norm_filename(d.title or "") in matched_norm
                    ]
                    if len(subs) == 1:
                        child = subs[0]
            if child:
                if _metadata_outranks_batch(child, encl.get("originator_type")):
                    logger.warning(
                        "Batch #%d doc #%d: skipping enclosure assignment — metadata "
                        "classified originator as %s but batch claims 'court'. Trusting metadata.",
                        batch_id,
                        child.id,
                        child.originator_type,
                    )
                    continue
                claimed_ids.add(child.id)
                child.role = DocumentRole.ENCLOSURE
                child.parent_id = cover_letter_doc_id
                child.originator_type = (
                    parse_originator_type(encl.get("originator_type"))
                    or child.originator_type
                )
                batch_originator = encl.get("attributed_originator")
                is_court = encl.get("originator_type", "").lower() == "court"
                if is_court or not child.attributed_originator:
                    child.attributed_originator = batch_originator

    # Cascade case/proceeding from any cover letter to all docs
    if first_cover and first_cover.case_id:
        for d in docs:
            if d.id not in claimed_ids and (not d.case_id or d.case_id == "_TRIAGE"):
                d.case_id = first_cover.case_id
                if first_cover.proceeding_id and not d.proceeding_id:
                    d.proceeding_id = first_cover.proceeding_id

    batch = db.query(IngestBatch).filter(IngestBatch.id == batch_id).first()
    if (
        batch
        and first_cover
        and first_cover.case_id
        and (not batch.case_id or batch.case_id == "_TRIAGE")
    ):
        batch.case_id = first_cover.case_id
        if first_cover.proceeding_id and not batch.proceeding_id:
            batch.proceeding_id = first_cover.proceeding_id

    case_id = batch.case_id if batch else None

    # Create action items from batch-level cross-document analysis
    if case_id and detected_actions:
        from app.services.intelligence.action_items import create_from_payload

        source_doc_id = first_cover.id if first_cover else None
        source_doc_date = first_cover.issued_date if first_cover else None
        create_from_payload(
            case_id,
            source_doc_id,
            batch.proceeding_id if batch else None,
            detected_actions,
            db,
            source_doc_date=source_doc_date,
        )

    # Single-relay fallback: when the AI didn't produce a bundle but exactly
    # one doc in the batch is flagged as a court relay (set in Phase 1 from
    # the letterhead), wire the unclaimed siblings as enclosures of that
    # relay. This is the common "court letter + attachments" shape that
    # doesn't read as a Begleitschreiben to the model.
    if not claimed_ids:
        relays = [d for d in docs if d.court_relay]
        if len(relays) == 1 and len(docs) > 1:
            relay = relays[0]
            relay.role = DocumentRole.COVER_LETTER
            for d in docs:
                if d.id == relay.id or d.parent_id is not None:
                    continue
                if _metadata_outranks_batch(d, "court"):
                    logger.warning(
                        "Batch #%d doc #%d: skipping single-relay fallback — metadata "
                        "classified originator as %s. Trusting metadata.",
                        batch_id,
                        d.id,
                        d.originator_type,
                    )
                    continue
                d.role = DocumentRole.ENCLOSURE
                d.parent_id = relay.id
                claimed_ids.add(d.id)

    # Mark unclaimed docs as STANDALONE
    for d in docs:
        if (
            d.id not in claimed_ids
            and not d.parent_id
            and d.role
            not in (
                DocumentRole.COVER_LETTER,
                DocumentRole.ENCLOSURE,
            )
        ):
            d.role = DocumentRole.STANDALONE

    db.commit()


def analyze(batch_id: int) -> bool:
    """Run the batch-level AI pass for the given IngestBatch.

    Returns True when the AI call ran, False when analysis was skipped
    (single doc or no healthy content). Raises on AI failure so the
    Celery task can retry and update the pipeline stage correctly.
    """
    db: Session = SessionLocal()
    try:
        cfg = get_chat_config(db)
        batch = db.query(IngestBatch).filter(IngestBatch.id == batch_id).first()
        if not batch:
            logger.warning(f"Batch {batch_id} not found for analysis")
            return False

        docs = db.query(Document).filter(Document.ingest_batch_id == batch_id).all()
        if not docs:
            logger.info(f"Batch {batch_id} has no documents to analyze")
            return False

        healthy_docs = [d for d in docs if d.content and len(d.content.strip()) > 10]

        if not healthy_docs or len(healthy_docs) == 1:
            for d in docs:
                d.role = DocumentRole.STANDALONE
            db.commit()
            return False

        candidate = _pick_cover_letter_candidate(healthy_docs)
        if not candidate:
            for d in docs:
                d.role = DocumentRole.STANDALONE
            db.commit()
            return False

        sibling_titles = [d.title for d in healthy_docs if d.id != candidate.id]

        try:
            result = _call_batch_analyzer_sync(
                candidate,
                sibling_titles,
                batch_id,
                model=cfg.summary_model,
                db=db,
            )
            _apply_batch_results(batch_id, docs, result, db)
            logger.info(f"Batch {batch_id} analyzed successfully")
        except Exception as e:
            logger.error(f"Batch {batch_id} analysis failed: {e}", exc_info=True)
            for d in docs:
                d.role = DocumentRole.STANDALONE
            db.commit()
            raise

        return True
    finally:
        db.close()
