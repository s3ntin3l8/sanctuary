import json
import logging
import os
from datetime import UTC, datetime

from sqlalchemy.orm import Session

from app.models.database import Document, Proceeding
from app.models.enums import OriginatorType
from app.services.ai_config import get_chat_config
from app.services.ingestion.extractors import (
    extract_case_id,
    extract_internal_id,
    infer_court_level,
    normalize_az_court,
)
from app.services.intelligence._ai_call import call_json_ai
from app.services.intelligence.ai_options import STAGE_OPTIONS
from app.services.intelligence.prompts import PHASE1_METADATA_SYSTEM
from app.services.intelligence.schemas import Phase1Metadata

logger = logging.getLogger(__name__)


def get_content_preview(doc: Document, max_chars: int = 60000) -> str:
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
    from app.services.ingestion.service import refresh_review_reasons

    # 1. Update core fields (AI overrides heuristics)
    if summary_data.get("sender"):
        doc.sender = summary_data["sender"]

    parsed_ot = parse_originator_type(summary_data.get("originator"))
    if parsed_ot is not None:
        doc.originator_type = parsed_ot

    # Fix 1A: court relay detection — court letterhead sender + non-court originator means
    # the court is merely forwarding a party submission.
    from app.models.enums import OriginatorType as _OT

    effective_sender = summary_data.get("sender") or doc.sender
    effective_ot = doc.originator_type
    if (
        effective_sender
        and infer_court_level(effective_sender) is not None
        and effective_ot is not None
        and effective_ot != _OT.COURT
    ):
        doc.court_relay = True

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
        "title",
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

    # Track strategy and character count for UI transparency
    content_len = len(doc.content or "")
    new_meta["ai_context_strategy"] = "windowed" if content_len > 60000 else "full"
    new_meta["ai_context_chars"] = len(get_content_preview(doc, 60000))

    doc.meta = new_meta

    # 3. Auto-Triage: internal_id leads (Case.id is primary identity per CLAUDE.md);
    #    az_court (Proceeding.az_court) is secondary context used as fallback.
    az_court = normalize_az_court(summary_data.get("az_court"))
    from app.core.validators import normalize_case_id

    internal_id = normalize_case_id(summary_data.get("internal_id"))
    ai_case_title = summary_data.get("case_title")
    # Discard case_title when it's just the internal_id echoed back (prompt drift)
    if ai_case_title and internal_id and ai_case_title.strip() == internal_id:
        ai_case_title = None

    # Persist the extracted reference so the triage form can display and edit it
    # independently of whether auto-triage resolves to an existing Case.
    if internal_id and not doc.internal_id:
        doc.internal_id = internal_id
    if az_court and not doc.az_court:
        doc.az_court = az_court

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

            # Derive court_name from Phase 1 sender when it looks like a court letterhead
            court_name_for_proc = (
                doc.sender
                if (doc.sender and infer_court_level(doc.sender) is not None)
                else None
            )

            draft_case, draft_proc, created = get_or_create_case_from_reference(
                db,
                internal_id=internal_id,
                az_court=az_court,
                court_name=court_name_for_proc,
                batch_subject=batch_subject,
                ai_case_title=ai_case_title,
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
    refresh_review_reasons(doc, db, commit=False)


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
    total_in_batch = (
        db.query(Document)
        .filter(Document.ingest_batch_id == doc.ingest_batch_id)
        .count()
    )

    # Only cascade to siblings when this is the only doc in the batch (BATCH_ANALYSIS is skipped)
    if total_in_batch == 1:
        for sib in siblings:
            sib.case_id = case.id
            if proceeding and not sib.proceeding_id:
                sib.proceeding_id = proceeding.id

        if siblings:
            logger.info(
                f"AI Auto-Triage: cascaded case {case.id} to "
                f"{len(siblings)} sibling doc(s) in batch {doc.ingest_batch_id}"
            )

    batch = db.query(IngestBatch).filter(IngestBatch.id == doc.ingest_batch_id).first()
    if batch and (not batch.case_id or batch.case_id == "_TRIAGE"):
        batch.case_id = case.id
        if proceeding and not batch.proceeding_id:
            batch.proceeding_id = proceeding.id


def generate_summary_sync(doc: Document, db=None) -> dict:
    """Synchronous version of generate_summary using configured AI provider."""
    cfg = get_chat_config(db)
    content_preview = get_content_preview(doc, 60000)

    # Heuristic hints for verification
    batch_subject = None
    batch_sender_email = None
    if doc.ingest_batch_id and db is not None:
        from app.models.database import IngestBatch

        row = (
            db.query(IngestBatch.subject, IngestBatch.sender_email)
            .filter(IngestBatch.id == doc.ingest_batch_id)
            .first()
        )
        if row:
            batch_subject, batch_sender_email = row
    safe_filename = os.path.basename(doc.file_path) if doc.file_path else ""
    content_for_hints = doc.content or ""

    az_hint = (
        doc.proceeding.az_court
        if doc.proceeding
        else extract_case_id(safe_filename, content_for_hints)["value"]
    )
    internal_id_hint = (
        doc.internal_id
        if doc.internal_id
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
        "email_subject": batch_subject if batch_sender_email else None,
    }
    hints = {k: v for k, v in hints.items() if v is not None}

    prompt = f"Document: {doc.title}\n\n"
    if hints:
        prompt += f"### Heuristic Hints (found by regex, please verify):\n{json.dumps(hints, indent=2)}\n\n"
    prompt += f"### Document Content Preview:\n{content_preview}"

    try:
        result = call_json_ai(
            system_prompt=PHASE1_METADATA_SYSTEM,
            user_prompt=prompt,
            options=STAGE_OPTIONS["metadata"],
            debug_label=f"doc_{doc.id}_sync",
            schema=Phase1Metadata,
            model=cfg.summary_model,
            db=db,
            ingest_batch_id=doc.ingest_batch_id,
            case_id=doc.case_id,
            two_pass=True,
        )
    except ValueError as e:
        if "empty response" in str(e):
            logger.info(
                "Doc %s metadata: empty AI response (thinking-only) — retrying without thinking",
                doc.id,
            )
            result = call_json_ai(
                system_prompt=PHASE1_METADATA_SYSTEM,
                user_prompt=prompt,
                options=STAGE_OPTIONS["metadata"],
                debug_label=f"doc_{doc.id}_syncretry",
                schema=Phase1Metadata,
                model=cfg.summary_model,
                db=db,
                ingest_batch_id=doc.ingest_batch_id,
                case_id=doc.case_id,
                suppress_thinking=True,
                two_pass=True,
            )
        else:
            raise
    logger.debug(f"AI response parsed for '{doc.title}'")
    return result.model_dump()


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
