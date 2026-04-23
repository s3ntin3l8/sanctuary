"""4a — Per-batch AI pass: cover-letter detection, originator attribution, action items."""

import logging
import re
from datetime import datetime

import httpx
from sqlalchemy.orm import Session

from app.config import DATA_DIR, SessionLocal
from app.core.async_utils import run_async
from app.models.database import ActionItem, Document, IngestBatch
from app.models.enums import (
    ActionItemStatus,
    ActionItemType,
    DocumentRole,
    OriginatorType,
)
from app.services.ai_config import get_effective_config
from app.services.ai_provider import ai_provider
from app.services.ai_summary import get_content_preview
from app.services.intelligence._json import parse_json_response
from app.services.intelligence.prompts import BATCH_ANALYZER_SYSTEM

logger = logging.getLogger(__name__)

COVER_LETTER_KEYWORDS = {
    "begleitschreiben",
    "anschreiben",
    "übersendungsschreiben",
    "deckblatt",
    "cover",
}

VALID_ORIGINATOR_TYPES = {e.value for e in OriginatorType}
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
    debug_file: str,
    model: str = "",
) -> dict:
    """Synchronous AI call for batch analysis."""
    content_preview = get_content_preview(candidate, 4000)
    sibling_list = "\n".join(f"- {t}" for t in sibling_titles)
    prompt = (
        f"Cover letter candidate (doc_id={candidate.id}):\n"
        f"Title: {candidate.title}\n\n"
        f"{content_preview}\n\n"
        f"Other documents in this batch:\n{sibling_list}"
    )

    import json

    params = run_async(
        ai_provider.get_generate_params(
            model=model or get_effective_config().summary_model,
            prompt=prompt,
            system_prompt=BATCH_ANALYZER_SYSTEM,
            stream=True,
            options={"num_ctx": 8192, "temperature": 0.1},
        )
    )
    ptype = run_async(ai_provider.get_type())

    full_response = ""
    with httpx.Client(timeout=httpx.Timeout(120.0, read=60.0)) as client:
        with open(debug_file, "a") as f:
            f.write(f"--- BATCH ANALYZER doc_id={candidate.id} ---\n")
            f.write(f"Payload: {json.dumps(params['json'])}\n\n")

        with client.stream(
            "POST", params["url"], json=params["json"], headers=params["headers"]
        ) as response:
            response.raise_for_status()
            for line in response.iter_lines():
                if not line:
                    continue
                chunk = ai_provider.parse_stream_line(line, ptype)
                if not chunk:
                    continue
                if "response" in chunk:
                    full_response += chunk["response"]
                if chunk.get("done"):
                    break

        with open(debug_file, "a") as f:
            f.write(f"\n--- END. Length: {len(full_response)} ---\n")

    if not full_response.strip():
        raise ValueError("Batch analyzer returned empty response")

    return parse_json_response(full_response)


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
        cover_letter_doc.attributed_originator = result.get("attributed_originator")

        def _norm(s: str) -> str:
            s = re.sub(r"\.[a-zA-Z]{2,5}$", "", s)  # strip extension
            return re.sub(r"[-_.\s]+", " ", s).lower().strip()

        for desc in enclosed_descriptions:
            matched = desc.get("matched_filename")
            child = None
            if matched:
                matched_norm = _norm(matched)
                child = next(
                    (
                        d
                        for d in docs
                        if d.id != cover_letter_doc_id
                        and (
                            matched_norm in _norm(d.title or "")
                            or _norm(d.title or "") in matched_norm
                        )
                    ),
                    None,
                )
            if child:
                child.role = DocumentRole.ENCLOSURE
                child.parent_id = cover_letter_doc_id
                raw_ot = (desc.get("originator_type") or "unknown").lower()
                if raw_ot in VALID_ORIGINATOR_TYPES:
                    child.originator_type = OriginatorType(raw_ot)
                child.attributed_originator = desc.get("attributed_originator")

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
                        created_at=datetime.now(),
                    )
                )
    else:
        for d in docs:
            d.role = DocumentRole.STANDALONE

    db.commit()


def analyze(batch_id: int) -> None:
    """Run the batch-level AI pass for the given IngestBatch."""
    db: Session = SessionLocal()
    try:
        cfg = get_effective_config(db)
        ai_provider.reload_from_db(db)
        batch = db.query(IngestBatch).filter(IngestBatch.id == batch_id).first()
        if not batch:
            logger.warning(f"Batch {batch_id} not found for analysis")
            return

        docs = db.query(Document).filter(Document.ingest_batch_id == batch_id).all()
        if not docs:
            logger.info(f"Batch {batch_id} has no documents to analyze")
            return

        healthy_docs = [d for d in docs if d.content and len(d.content.strip()) > 10]

        if not healthy_docs:
            for d in docs:
                d.role = DocumentRole.STANDALONE
            db.commit()
            return

        if len(healthy_docs) == 1:
            for d in docs:
                d.role = DocumentRole.STANDALONE
            db.commit()
            return

        candidate = _pick_cover_letter_candidate(healthy_docs)
        if not candidate:
            for d in docs:
                d.role = DocumentRole.STANDALONE
            db.commit()
            return

        debug_dir = DATA_DIR / "ai_debug"
        debug_dir.mkdir(parents=True, exist_ok=True)
        debug_file = str(debug_dir / f"batch_{batch_id}_analyzer.log")

        sibling_titles = [d.title for d in healthy_docs if d.id != candidate.id]

        try:
            result = _call_batch_analyzer_sync(
                candidate, sibling_titles, debug_file, model=cfg.summary_model
            )
            _apply_batch_results(batch_id, docs, result, db)
            logger.info(f"Batch {batch_id} analyzed successfully")
        except Exception as e:
            logger.error(f"Batch {batch_id} analysis failed: {e}", exc_info=True)
            for d in docs:
                d.role = DocumentRole.STANDALONE
            db.commit()
    finally:
        db.close()
