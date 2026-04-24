import json
import logging
import os
from datetime import UTC, datetime

from sqlalchemy.orm import Session

from app.models.database import Document, Proceeding
from app.models.enums import OriginatorType
from app.services.ai_config import get_effective_config
from app.services.ingestion.extractors import extract_case_id, extract_internal_id
from app.services.intelligence._ai_call import call_json_ai
from app.services.intelligence.ai_options import STAGE_OPTIONS
from app.services.intelligence.prompts import PHASE1_METADATA_SYSTEM

logger = logging.getLogger(__name__)


def get_content_preview(
    doc: Document, max_chars: int = 60000, include_tail: bool = True
) -> str:
    """Get a representative preview of document content using proportional windowing.

    When content exceeds max_chars, returns a composite view:
    - Head: 25% of max_chars
    - Middle: 50% of max_chars (centered)
    - Tail: 25% of max_chars
    """
    content = doc.content or ""

    # If content fits within max_chars, return as-is
    if len(content) <= max_chars:
        return content

    # Calculate window sizes
    head_size = int(max_chars * 0.25)
    tail_size = int(max_chars * 0.25)
    mid_size = max_chars - head_size - tail_size

    head = content[:head_size]

    # Center the middle window
    mid_start = (len(content) // 2) - (mid_size // 2)
    mid = content[mid_start : mid_start + mid_size]

    tail = content[-tail_size:]

    separator = "\n\n[... Omitted for brevity ...]\n\n"
    return f"{head}{separator}{mid}{separator}{tail}"


def enrich_document_with_ai(doc: Document, summary_data: dict, db: Session) -> None:
    """Refine document properties based on deep AI extraction."""
    from app.models.database import Case
    from app.models.enums import parse_originator_type
    from app.services.ingestion.service import compute_review_reasons

    # 1. Update core fields (AI overrides heuristics)
    if summary_data.get("sender"):
        doc.sender = summary_data["sender"]

    parsed_ot = parse_originator_type(summary_data.get("originator"))
    if parsed_ot is not None:
        doc.originator_type = parsed_ot

    if summary_data.get("issued_date"):
        raw_date = str(summary_data["issued_date"]).strip()
        parsed_date = None
        for fmt in ("%Y-%m-%d", "%d.%m.%Y", "%d.%m.%y"):
            try:
                parsed_date = datetime.strptime(raw_date[:10], fmt)
                break
            except ValueError:
                pass
        if parsed_date is None:
            # ISO-8601 with time component: "2025-03-11T00:00:00"
            try:
                parsed_date = datetime.fromisoformat(raw_date)
            except ValueError:
                pass
        if parsed_date is not None:
            doc.issued_date = parsed_date.replace(tzinfo=UTC)

    # 2. Update confidence scores — only accept schema keys; drop unknown ones
    #    (e.g. case_id) so prompt drift can't inject bogus confidence values.
    _KNOWN_CONF_KEYS = {
        "sender",
        "issued_date",
        "originator",
        "az_court",
        "internal_id",
    }
    ai_conf = summary_data.get("confidence")
    if ai_conf and isinstance(ai_conf, dict):
        new_conf = dict(doc.extraction_confidence or {})
        for key, val in ai_conf.items():
            if not isinstance(val, str):
                continue
            v = val.strip().lower()
            if v not in ("high", "medium", "low"):
                continue
            if key not in _KNOWN_CONF_KEYS:
                continue
            new_conf[key] = v
        doc.extraction_confidence = new_conf

    # Update intelligence flags in meta
    new_meta = dict(doc.meta or {})
    contradictions = summary_data.get("contradictions", [])
    if contradictions:
        new_meta["ai_contradiction"] = True
        new_meta["contradiction_notes"] = contradictions
    else:
        new_meta["ai_contradiction"] = False
    doc.meta = new_meta

    # 3. Auto-Triage: internal_id leads (Case.id is primary identity per CLAUDE.md);
    #    az_court (Proceeding.az_court) is secondary context used as fallback.
    az_court = summary_data.get("az_court")
    internal_id = summary_data.get("internal_id")
    if internal_id and isinstance(internal_id, str):
        internal_id = internal_id.replace("/", "-").strip()

    if doc.case_id == "_TRIAGE":
        matching_case = None
        matching_proceeding = None

        if internal_id:
            matching_case = db.query(Case).filter(Case.id == internal_id).first()
            if matching_case and az_court:
                matching_proceeding = (
                    db.query(Proceeding)
                    .filter(
                        Proceeding.case_id == matching_case.id,
                        Proceeding.az_court == az_court,
                    )
                    .first()
                )

        if not matching_case and az_court:
            matching_proceeding = (
                db.query(Proceeding).filter(Proceeding.az_court == az_court).first()
            )
            if matching_proceeding:
                matching_case = matching_proceeding.case

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
            _cascade_case_to_batch(db, doc, matching_case, matching_proceeding)

        elif internal_id:
            # No existing case matched — auto-create a draft for user confirmation.
            from app.services.case_service import get_or_create_case_from_reference

            batch_subject = doc.ingest_batch.subject if doc.ingest_batch else None
            draft_case, draft_proc, created = get_or_create_case_from_reference(
                db,
                internal_id=internal_id,
                az_court=az_court,
                batch_subject=batch_subject,
                is_draft=True,
            )
            db.flush()
            doc.case_id = draft_case.id
            if draft_proc:
                doc.proceeding_id = draft_proc.id
            _cascade_case_to_batch(db, doc, draft_case, draft_proc)
            if created:
                logger.info(
                    f"AI Auto-Triage: draft case {draft_case.id} created for doc {doc.id}"
                )

    # 4. Re-evaluate review status
    reasons = compute_review_reasons(doc, confirmed=False)
    doc.review_reasons = reasons
    doc.needs_review = len(reasons) > 0


def _cascade_case_to_batch(db, doc: Document, case, proceeding) -> None:
    """Cascade a case assignment to the ingest batch and (single-doc only) siblings.

    For multi-doc batches: BATCH_ANALYSIS is the cross-doc authority and runs after all
    METADATA tasks finish. METADATA cascades to the batch record only — sibling docs are left
    with _TRIAGE so BATCH_ANALYSIS can assign the cover letter's case.
    For single-doc / unbatched docs: cascade to siblings (there are none) and the batch.
    """
    if not doc.ingest_batch_id:
        return
    from app.models.database import IngestBatch

    # For batched docs: BATCH_ANALYSIS runs after all METADATA complete.
    # METADATA assigns the doc's own case_id but does NOT overwrite sibling case_ids —
    # BATCH_ANALYSIS is the authoritative cascade for multi-doc batches.
    siblings = (
        db.query(Document)
        .filter(
            Document.ingest_batch_id == doc.ingest_batch_id,
            Document.case_id == "_TRIAGE",
            Document.id != doc.id,
        )
        .all()
    )
    # Only cascade to siblings when this is the only doc in the batch (BATCH_ANALYSIS is skipped)
    for sib in siblings:
        sib.case_id = case.id
        if proceeding and not sib.proceeding_id:
            sib.proceeding_id = proceeding.id

    batch = db.query(IngestBatch).filter(IngestBatch.id == doc.ingest_batch_id).first()
    if batch and (not batch.case_id or batch.case_id == "_TRIAGE"):
        batch.case_id = case.id
        if proceeding and not batch.proceeding_id:
            batch.proceeding_id = proceeding.id

    if siblings:
        logger.info(
            f"AI Auto-Triage: cascaded case {case.id} to "
            f"{len(siblings)} sibling doc(s) in batch {doc.ingest_batch_id}"
        )


def generate_summary_sync(doc: Document, db=None) -> dict:
    """Synchronous version of generate_summary using configured AI provider."""
    cfg = get_effective_config(db)
    content_preview = get_content_preview(doc, 4000)

    # Heuristic hints for verification
    batch_subject = None
    if doc.ingest_batch_id and db is not None:
        from app.models.database import IngestBatch

        batch_subject = (
            db.query(IngestBatch.subject)
            .filter(IngestBatch.id == doc.ingest_batch_id)
            .scalar()
        )
    safe_filename = os.path.basename(doc.file_path) if doc.file_path else ""
    content_for_hints = doc.content or ""

    az_hint = (
        doc.proceeding.az_court
        if doc.proceeding
        else extract_case_id(safe_filename, content_for_hints)["value"]
    )
    internal_id_hint = (
        doc.case_id
        if doc.case_id and doc.case_id != "_TRIAGE"
        else extract_internal_id(content_for_hints)["value"]
    )

    hints = {
        "az_court": az_hint,
        "internal_id": internal_id_hint,
        "sender": doc.sender,
        "issued_date": doc.issued_date.strftime("%Y-%m-%d")
        if doc.issued_date
        else None,
        "originator": (
            doc.originator_type.value
            if doc.originator_type and doc.originator_type != OriginatorType.UNKNOWN
            else None
        ),
        "email_subject": batch_subject,
    }
    hints = {k: v for k, v in hints.items() if v is not None}

    prompt = f"Document: {doc.title}\n\n"
    if hints:
        prompt += f"### Heuristic Hints (found by regex, please verify):\n{json.dumps(hints, indent=2)}\n\n"
    prompt += f"### Document Content Preview:\n{content_preview}"

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

        # Phase 1: only apply metadata fields (az_court, sender, issued_date, originator_type)
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
