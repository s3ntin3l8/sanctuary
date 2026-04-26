"""4a — Per-batch AI pass: cover-letter detection, originator attribution, action items.

Supports multi-bundle format (new):
- bundles: [{"cover_letter_doc_id": int|null, "enclosed": [...]}]
- Each bundle wires its enclosures to its cover letter

Legacy format (backward compat):
- cover_letter_doc_id, is_cover_letter, enclosed_descriptions
"""

import logging
import re
from datetime import datetime

from sqlalchemy.orm import Session

from app.config import SessionLocal
from app.models.database import ActionItem, Document, IngestBatch
from app.models.enums import (
    ActionItemStatus,
    ActionItemType,
    DocumentRole,
    parse_originator_type,
)
from app.services.ai_config import get_effective_config
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

VALID_ACTION_TYPES = {e.value for e in ActionItemType}


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
                    claimed_ids.add(child.id)
                    child.role = DocumentRole.ENCLOSURE
                    child.parent_id = cover_id
                    child.originator_type = (
                        parse_originator_type(encl.get("originator_type"))
                        or child.originator_type
                    )
                    child.attributed_originator = encl.get("attributed_originator")
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
                claimed_ids.add(child.id)
                child.role = DocumentRole.ENCLOSURE
                child.parent_id = cover_letter_doc_id
                child.originator_type = (
                    parse_originator_type(encl.get("originator_type"))
                    or child.originator_type
                )
                child.attributed_originator = encl.get("attributed_originator")

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

    # Create action items
    if case_id:
        for action in detected_actions:
            raw_type = (action.get("action_type") or "deadline").lower()
            if raw_type not in VALID_ACTION_TYPES:
                raw_type = "deadline"
            due_str = action.get("due_date")
            try:
                due_date = datetime.strptime(due_str, "%Y-%m-%d") if due_str else None
            except ValueError:
                due_date = None
            if not due_date:
                continue

            source_doc_id = first_cover.id if first_cover else None
            db.add(
                ActionItem(
                    case_id=case_id,
                    proceeding_id=batch.proceeding_id if batch else None,
                    source_document_id=source_doc_id,
                    title=action.get("title", "Extracted action item")[:255],
                    description=action.get("description"),
                    due_date=due_date,
                    action_type=ActionItemType(raw_type),
                    status=ActionItemStatus.OPEN,
                    ingest_date=datetime.now(),
                )
            )

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
        cfg = get_effective_config(db)
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
                candidate, sibling_titles, batch_id, model=cfg.summary_model, db=db
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
