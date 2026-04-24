import json
import logging
from datetime import UTC, datetime

from sqlalchemy.orm import Session

from app.models.database import Document, Proceeding
from app.services.ai_config import get_effective_config
from app.services.intelligence._ai_call import call_json_ai
from app.services.intelligence.ai_options import _TAIL_CHARS, STAGE_OPTIONS
from app.services.intelligence.prompts import PHASE1_METADATA_SYSTEM

logger = logging.getLogger(__name__)


def get_content_preview(
    doc: Document, max_chars: int = 4000, include_tail: bool = True
) -> str:
    """Get a representative preview of document content using chunks if available.

    When include_tail=True and content exceeds max_chars, returns a head+tail window.
    Output length may exceed max_chars by the length of the truncation marker."""
    if doc.meta and "chunks" in doc.meta and doc.meta["chunks"]:
        content = ""
        current_len = 0
        for chunk in doc.meta["chunks"]:
            text = chunk.get("text", "")
            if current_len + len(text) > max_chars:
                break
            content += text + "\n\n"
            current_len += len(text)
        if content:
            return content

    content = doc.content or ""

    # If content fits within max_chars, return as-is
    if len(content) <= max_chars:
        return content

    # For long content with include_tail, use head+tail window
    if include_tail and max_chars > _TAIL_CHARS:
        head_chars = max_chars - _TAIL_CHARS
        return (
            content[:head_chars]
            + "\n\n[... truncated middle ...]\n\n"
            + content[-_TAIL_CHARS:]
        )

    # Fallback: return head-only (include_tail=False or max_chars <= _TAIL_CHARS)
    return content[:max_chars]


def enrich_document_with_ai(doc: Document, summary_data: dict, db: Session) -> None:
    """Refine document properties based on deep AI extraction."""
    from app.models.database import Case
    from app.models.enums import parse_originator_type
    from app.services.ingestion.service import compute_review_reasons

    # 1. Update core fields (AI overrides heuristics)
    if summary_data.get("sender"):
        doc.sender = summary_data["sender"]

    parsed_ot = parse_originator_type(summary_data.get("originator_type"))
    if parsed_ot is not None:
        doc.originator_type = parsed_ot

    if summary_data.get("received_date"):
        try:
            parsed_date = datetime.strptime(summary_data["received_date"], "%Y-%m-%d")
            doc.received_date = parsed_date.replace(tzinfo=UTC)
        except Exception:
            pass

    if summary_data.get("internal_id"):
        doc.internal_id = summary_data["internal_id"]

    # 2. Update confidence scores
    ai_conf = summary_data.get("confidence")
    if ai_conf and isinstance(ai_conf, dict):
        # Create a new dict to ensure SQLAlchemy detects the change
        new_conf = dict(doc.extraction_confidence or {})
        for key, val in ai_conf.items():
            if val in ("high", "medium", "low"):
                new_conf[key] = val
        doc.extraction_confidence = new_conf

    # 3. Auto-Triage: two distinct signals, tried in order:
    #   - az_court  → matches Proceeding.az_court (per-court Aktenzeichen, context identifier)
    #   - internal_id → matches Case.id (internal primary identity per CLAUDE.md)
    az_court = summary_data.get("az_court")
    internal_id = summary_data.get("internal_id")

    if doc.case_id == "_TRIAGE":
        matching_case = None
        matching_proceeding = None

        if az_court:
            matching_proceeding = (
                db.query(Proceeding).filter(Proceeding.az_court == az_court).first()
            )
            if matching_proceeding:
                matching_case = matching_proceeding.case

        if not matching_case and internal_id:
            matching_case = db.query(Case).filter(Case.id == internal_id).first()

        if matching_case:
            doc.case_id = matching_case.id
            if matching_proceeding:
                doc.proceeding_id = matching_proceeding.id
            logger.info(
                f"AI Auto-Triage: moved doc {doc.id} to case {matching_case.id}"
                + (
                    f" / proceeding {matching_proceeding.id}"
                    if matching_proceeding
                    else ""
                )
            )

    # 4. Re-evaluate review status
    reasons = compute_review_reasons(doc)
    doc.review_reasons = reasons
    doc.needs_review = len(reasons) > 0


def generate_summary_sync(doc: Document, db=None) -> dict:
    """Synchronous version of generate_summary using configured AI provider."""
    cfg = get_effective_config(db)
    content_preview = get_content_preview(doc, 4000)

    # Heuristic hints for verification
    hints = {
        "az_court": doc.proceeding.az_court if doc.proceeding else None,
        "sender": doc.sender,
        "received_date": doc.received_date.strftime("%Y-%m-%d")
        if doc.received_date
        else None,
        "originator_type": doc.originator_type.value if doc.originator_type else None,
    }
    hints_str = json.dumps(hints, indent=2)

    prompt = (
        f"Document: {doc.title}\n\n"
        f"### Heuristic Hints (found by regex, please verify):\n{hints_str}\n\n"
        f"### Document Content Preview:\n{content_preview}"
    )

    result = call_json_ai(
        system_prompt=PHASE1_METADATA_SYSTEM,
        user_prompt=prompt,
        options=STAGE_OPTIONS["metadata"],
        debug_label=f"doc_{doc.id}_sync",
        model=cfg.summary_model,
        db=db,
        ingest_batch_id=doc.ingest_batch_id,
    )
    logger.debug(f"AI response parsed for '{doc.title}'")
    return result


def _summarize_document_sync(doc_id: int, db: Session) -> Document:
    """Synchronous wrapper for fire-and-forget background summary generation."""
    doc = db.query(Document).filter(Document.id == doc_id).first()
    if not doc or not doc.content or doc.content.startswith("Conversion failed:"):
        return doc

    db.commit()

    try:
        summary_data = generate_summary_sync(doc, db=db)

        # Phase 1: only apply metadata fields (az_court, sender, received_date, originator_type)
        # The 3-bullet ai_summary is now written by Phase 4 document_enricher
        enrich_document_with_ai(doc, summary_data, db)
    except Exception as e:
        logger.error(
            f"Failed to generate summary for doc {doc_id} (sync): {e}", exc_info=True
        )
        doc.ai_summary = {"error": str(e)}
        db.commit()
        raise

    db.commit()
    db.refresh(doc)
    return doc
