"""4a — Per-batch AI pass: cover-letter detection, originator attribution, action items."""

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
    content_preview = get_content_preview(candidate, 4000, include_tail=False)
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


def _apply_batch_results(
    batch_id: int,
    docs: list[Document],
    result: dict,
    db: Session,
) -> None:
    """Write batch analyzer results to the DB."""
    cover_letter_doc_id = result.get("cover_letter_doc_id")
    is_cover_letter = result.get("is_cover_letter", False)
    court_relay = result.get("court_relay", False)
    enclosed_descriptions = result.get("enclosed_descriptions") or []
    detected_actions = result.get("detected_actions") or []

    doc_map = {d.id: d for d in docs}

    cover_letter_doc = doc_map.get(cover_letter_doc_id) if cover_letter_doc_id else None

    if cover_letter_doc and is_cover_letter:
        cover_letter_doc.role = DocumentRole.COVER_LETTER
        cover_letter_doc.court_relay = bool(court_relay)
        # Derive cover-letter originator from the first enclosed document — the prompt
        # schema only emits attributed_originator inside enclosed_descriptions[], never
        # at the top level.
        cover_letter_doc.attributed_originator = next(
            (
                desc.get("attributed_originator")
                for desc in enclosed_descriptions
                if desc.get("attributed_originator")
            ),
            None,
        )

        def _norm(s: str) -> str:
            s = re.sub(r"\.[a-zA-Z]{2,5}$", "", s)  # strip extension
            return re.sub(r"[-_.\s]+", " ", s).lower().strip()

        claimed_ids: set[int] = set()
        for desc in enclosed_descriptions:
            matched = desc.get("matched_filename")
            child = None
            if matched:
                matched_norm = _norm(matched)
                candidates = [
                    d
                    for d in docs
                    if d.id != cover_letter_doc_id and d.id not in claimed_ids
                ]
                # Exact normalized match wins; only fall back to substring when unambiguous.
                child = next(
                    (d for d in candidates if _norm(d.title or "") == matched_norm),
                    None,
                )
                if not child:
                    subs = [
                        d
                        for d in candidates
                        if matched_norm in _norm(d.title or "")
                        or _norm(d.title or "") in matched_norm
                    ]
                    if len(subs) == 1:
                        child = subs[0]
            if child:
                claimed_ids.add(child.id)
                child.role = DocumentRole.ENCLOSURE
                child.parent_id = cover_letter_doc_id
                parsed_ot = parse_originator_type(desc.get("originator_type"))
                if parsed_ot is not None:
                    child.originator_type = parsed_ot
                child.attributed_originator = desc.get("attributed_originator")

        # Cascade cover-letter's case/proceeding to all sibling docs and the batch.
        if cover_letter_doc.case_id and cover_letter_doc.case_id != "_TRIAGE":
            for d in docs:
                if d.id != cover_letter_doc_id and (
                    not d.case_id or d.case_id == "_TRIAGE"
                ):
                    d.case_id = cover_letter_doc.case_id
                    if cover_letter_doc.proceeding_id and not d.proceeding_id:
                        d.proceeding_id = cover_letter_doc.proceeding_id
            batch = db.query(IngestBatch).filter(IngestBatch.id == batch_id).first()
            if batch and (not batch.case_id or batch.case_id == "_TRIAGE"):
                batch.case_id = cover_letter_doc.case_id
                if cover_letter_doc.proceeding_id and not batch.proceeding_id:
                    batch.proceeding_id = cover_letter_doc.proceeding_id
        else:
            batch = db.query(IngestBatch).filter(IngestBatch.id == batch_id).first()
        case_id = batch.case_id if batch else None

        if case_id:
            for action in detected_actions:
                raw_type = (action.get("action_type") or "deadline").lower()
                if raw_type not in VALID_ACTION_TYPES:
                    raw_type = "deadline"
                due_str = action.get("due_date")
                try:
                    due_date = (
                        datetime.strptime(due_str, "%Y-%m-%d") if due_str else None
                    )
                except ValueError:
                    due_date = None
                if not due_date:
                    continue

                db.add(
                    ActionItem(
                        case_id=case_id,
                        proceeding_id=batch.proceeding_id if batch else None,
                        source_document_id=cover_letter_doc_id,
                        title=action.get("title", "Extracted action item")[:255],
                        description=action.get("description"),
                        due_date=due_date,
                        action_type=ActionItemType(raw_type),
                        status=ActionItemStatus.OPEN,
                        ingest_date=datetime.now(),
                    )
                )
    else:
        for d in docs:
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
            raise  # propagate so analyze_batch_task can retry and mark stage FAILED

        return True
    finally:
        db.close()
