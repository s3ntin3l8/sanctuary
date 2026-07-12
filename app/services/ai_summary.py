import json
import logging
import re
from datetime import UTC, datetime

from sqlalchemy.orm import Session

from app.models.database import Document, Proceeding
from app.models.enums import OriginatorType, ProceedingCourtLevel, ProceedingStatus
from app.services.ai_config import get_chat_config
from app.services.ingestion.extractors import (
    extract_internal_id,
    infer_court_level,
    normalize_az_court,
)
from app.services.intelligence._ai_call import call_json_ai
from app.services.intelligence._party_context import format_party_context
from app.services.intelligence.ai_options import STAGE_OPTIONS
from app.services.intelligence.prompts import PHASE1_METADATA_SYSTEM, fence
from app.services.intelligence.schemas import Phase1Metadata

logger = logging.getLogger(__name__)

_LAW_FIRM_INDICATORS = re.compile(
    r"\b(?:rechtsanw[äa]lt(?:e|in)?|kanzlei|partnerschaft|partner\b)",
    re.IGNORECASE,
)

# Strip Docling-rendered markdown image alt-text that occasionally leaks into
# the AI-extracted `sender` when the document's first visible token is a red
# court stamp rendered as ` ![Red stamp of …](…) `. Pure transport-layer
# pollution removal — not an AI judgment override.
_MARKDOWN_IMAGE_RE = re.compile(r"!\[[^\]]*\]\([^)]*\)")


def _sanitize_sender(raw: str | None) -> str | None:
    """Strip markdown image alt-text from an AI-emitted sender string.

    Returns None when the entire string was alt-text (so the caller can leave
    `doc.sender` unchanged rather than overwriting a clean prior value with
    garbage). Also normalizes whitespace.
    """
    if not raw or not isinstance(raw, str):
        return raw
    cleaned = _MARKDOWN_IMAGE_RE.sub("", raw)
    cleaned = re.sub(r"\s+", " ", cleaned).strip()
    return cleaned or None


def _looks_like_court(name: str | None) -> bool:
    """Return False if name looks like a law firm rather than a court."""
    if not name:
        return False
    return not (_LAW_FIRM_INDICATORS.search(name) and "gericht" not in name.lower())


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


def _apply_proceeding_extraction(
    doc: Document, summary_data: dict, db: Session
) -> str | None:
    """Create or update the Proceeding for this document based on METADATA extraction.

    Returns a skip reason string, or None on success.
    """
    from app.services.ingestion.extractors import infer_court_level as _infer_level

    if not doc.case_id or doc.case_id == "_TRIAGE":
        return "no case assigned"

    data = dict(summary_data)

    # Fallback: AI flagged not-court but metadata signals court origin.
    if (
        not doc.proceeding_id
        and not data.get("is_court_document")
        and doc.az_court
        and doc.originator_type == OriginatorType.COURT
        and _looks_like_court(doc.sender)
    ):
        inferred_level = _infer_level(doc.sender)
        court_name = (
            data.get("court_name")
            if _looks_like_court(data.get("court_name"))
            else (doc.sender if _looks_like_court(doc.sender) else None)
        )
        data = {
            **data,
            "is_court_document": True,
            "az_court": doc.az_court,
            "court_name": court_name,
            "court_level": data.get("court_level")
            or (inferred_level.value if inferred_level else None),
        }
        logger.info(
            "Doc %d: proceeding AI empty/uncertain — falling back to Document.az_court=%s.",
            doc.id,
            doc.az_court,
        )

    if not data.get("is_court_document"):
        return "not a court document"

    extracted_az = normalize_az_court(data.get("az_court"))

    # Secondary fallback: court doc but invalid AZ — use METADATA hint.
    if (
        not doc.proceeding_id
        and data.get("is_court_document")
        and not extracted_az
        and doc.az_court
        and doc.originator_type == OriginatorType.COURT
    ):
        extracted_az = normalize_az_court(doc.az_court)
        if extracted_az:
            logger.info(
                "Doc %d: invalid az_court=%r — falling back to Document.az_court=%s.",
                doc.id,
                data.get("az_court"),
                extracted_az,
            )

    extracted_level_str = data.get("court_level")
    try:
        extracted_level = (
            ProceedingCourtLevel(extracted_level_str.lower())
            if extracted_level_str
            else None
        )
    except ValueError:
        extracted_level = None

    if not doc.proceeding_id:
        existing = None
        if extracted_az:
            existing = (
                db.query(Proceeding)
                .filter(
                    Proceeding.case_id == doc.case_id,
                    Proceeding.az_court == extracted_az,
                )
                .first()
            )
        if existing:
            doc.proceeding_id = existing.id
        else:
            placeholder = None
            if extracted_az:
                placeholder = (
                    db.query(Proceeding)
                    .filter(
                        Proceeding.case_id == doc.case_id,
                        Proceeding.court_name.in_(["General", "Unknown Court"]),
                        Proceeding.az_court.is_(None),
                    )
                    .first()
                )
            if placeholder:
                placeholder.az_court = extracted_az
                if extracted_level:
                    placeholder.court_level = extracted_level
                if data.get("court_name") and _looks_like_court(data.get("court_name")):
                    placeholder.court_name = data["court_name"]
                if data.get("subject_matter") and not placeholder.subject_matter:
                    placeholder.subject_matter = data["subject_matter"]
                doc.proceeding_id = placeholder.id
            elif extracted_az:
                new_proc = Proceeding(
                    case_id=doc.case_id,
                    court_name=data.get("court_name")
                    if _looks_like_court(data.get("court_name"))
                    else "Unknown Court",
                    court_level=extracted_level or ProceedingCourtLevel.AG,
                    az_court=extracted_az,
                    subject_matter=data.get("subject_matter"),
                    status=ProceedingStatus.ACTIVE,
                    started_at=datetime.now(UTC),
                    is_draft=True,
                )
                db.add(new_proc)
                db.flush()
                doc.proceeding_id = new_proc.id
            else:
                return "no az extracted"

        if doc.ingest_batch and not doc.ingest_batch.proceeding_id:
            doc.ingest_batch.proceeding_id = doc.proceeding_id

        # Infer case type from the AZ letter code (e.g. "3 F 426/25" → FAMILY)
        if extracted_az and doc.case_id:
            from app.models.database import Case
            from app.services.case_service import _maybe_set_case_type_from_az

            parent_case = db.query(Case).filter(Case.id == doc.case_id).first()
            if parent_case:
                _maybe_set_case_type_from_az(parent_case, extracted_az)

        return None

    current_proc = (
        db.query(Proceeding).filter(Proceeding.id == doc.proceeding_id).first()
    )
    if not current_proc:
        return "proceeding not found"

    is_new_instance = False
    levels = list(ProceedingCourtLevel)

    if extracted_level and current_proc.court_level:
        try:
            if levels.index(extracted_level) > levels.index(current_proc.court_level):
                is_new_instance = True
        except ValueError:
            pass

    if extracted_az and current_proc.az_court and extracted_az != current_proc.az_court:
        is_new_instance = True

    if is_new_instance:
        existing_match = None
        if extracted_az:
            existing_match = (
                db.query(Proceeding)
                .filter(
                    Proceeding.case_id == current_proc.case_id,
                    Proceeding.az_court == extracted_az,
                    Proceeding.id != current_proc.id,
                )
                .first()
            )

        if existing_match:
            new_proc = existing_match
            if current_proc.az_court != extracted_az:
                current_proc.status = ProceedingStatus.CLOSED
                current_proc.ended_at = datetime.now(UTC)
        else:
            new_proc = Proceeding(
                case_id=current_proc.case_id,
                court_name=data.get("court_name")
                if _looks_like_court(data.get("court_name"))
                else "Unknown Court",
                court_level=extracted_level or ProceedingCourtLevel.AG,
                az_court=extracted_az,
                subject_matter=data.get("subject_matter"),
                status=ProceedingStatus.ACTIVE,
                started_at=datetime.now(UTC),
                is_draft=True,
            )
            db.add(new_proc)
            current_proc.status = ProceedingStatus.CLOSED
            current_proc.ended_at = datetime.now(UTC)
            db.flush()

        doc.proceeding_id = new_proc.id
        if doc.ingest_batch:
            doc.ingest_batch.proceeding_id = new_proc.id
            for batch_doc in doc.ingest_batch.documents:
                batch_doc.proceeding_id = new_proc.id
    else:
        if not current_proc.az_court and extracted_az:
            current_proc.az_court = extracted_az
        extracted_court_name = data.get("court_name")
        if (
            (
                not current_proc.court_name
                or current_proc.court_name in ("Unknown Court", "General")
            )
            and extracted_court_name
            and _looks_like_court(extracted_court_name)
        ):
            current_proc.court_name = extracted_court_name
        if not current_proc.subject_matter and data.get("subject_matter"):
            current_proc.subject_matter = data.get("subject_matter")
        if extracted_level and current_proc.court_level in (
            ProceedingCourtLevel.AG,
            ProceedingCourtLevel.OTHER,
        ):
            current_proc.court_level = extracted_level

    # Infer case type from the AZ letter code whenever we have one
    if extracted_az and doc.case_id:
        from app.models.database import Case
        from app.services.case_service import _maybe_set_case_type_from_az

        parent_case = db.query(Case).filter(Case.id == doc.case_id).first()
        if parent_case:
            _maybe_set_case_type_from_az(parent_case, extracted_az)

    return None


def enrich_document_with_ai(doc: Document, summary_data: dict, db: Session) -> None:
    """Refine document properties based on deep AI extraction."""
    from app.models.database import Case
    from app.models.enums import parse_originator_type
    from app.services.ingestion.service import refresh_review_reasons
    from app.services.intelligence._court_identity import reconcile_ai_fields

    # Resolve self-contradictions in the AI output before writing any fields.
    # Mutates summary_data in place; logs any rule that fired.
    reconcile_ai_fields(doc, summary_data)

    # 1. Update core fields (AI is authoritative)
    sender_raw = summary_data.get("sender")
    cleaned_sender = _sanitize_sender(sender_raw)
    if cleaned_sender:
        doc.sender = cleaned_sender

    parsed_ot = parse_originator_type(summary_data.get("originator_type"))
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
        "originator_type",
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

            # Prefer AI-extracted court_name; fall back to sender heuristic.
            court_name_for_proc = (
                summary_data.get("court_name")
                if _looks_like_court(summary_data.get("court_name"))
                else (
                    doc.sender
                    if (doc.sender and infer_court_level(doc.sender) is not None)
                    else None
                )
            )

            draft_case, draft_proc, created = get_or_create_case_from_reference(
                db,
                internal_id=internal_id,
                az_court=az_court,
                court_name=court_name_for_proc,
                batch_subject=batch_subject,
                ai_case_title=ai_case_title,
                is_draft=True,
                # AI-auto-created draft cases are owned by whoever ingested the
                # document (so they appear in that user's case directory).
                owner_id=doc.owner_id,
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

    # 5. Create/update Proceeding (merged from former PROCEEDING_ANALYSIS stage).
    #    Must run after auto-triage above so doc.case_id is set.
    skip_reason = _apply_proceeding_extraction(doc, summary_data, db)
    if skip_reason:
        logger.debug("Doc %d: proceeding extraction skipped: %s", doc.id, skip_reason)

    # 6. AI case_type fallback — only fires when AZ inference (step 5) left the
    #    case at the CIVIL default AND the AI extracted a non-CIVIL type from text.
    ai_ct_raw = summary_data.get("case_type")
    if ai_ct_raw and doc.case_id and doc.case_id != "_TRIAGE":
        from app.models.database import Case
        from app.models.enums import CaseType, parse_case_type

        parsed_ct = parse_case_type(ai_ct_raw)
        if parsed_ct and parsed_ct != CaseType.CIVIL:
            ai_case = db.query(Case).filter(Case.id == doc.case_id).first()
            if ai_case and ai_case.case_type == CaseType.CIVIL:
                ai_case.case_type = parsed_ct
                if parsed_ct == CaseType.FAMILY and ai_case.assume_worst_case is True:
                    ai_case.assume_worst_case = False
                logger.info(
                    "Doc %d: AI inferred case_type=%s for case %s",
                    doc.id,
                    parsed_ct,
                    doc.case_id,
                )


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


def generate_summary_sync(
    doc: Document, db=None, model: str = "", base_url: str | None = None
) -> dict:
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
    content_for_hints = doc.content or ""

    az_hint = doc.proceeding.az_court if doc.proceeding else None
    internal_id_hint = (
        doc.internal_id
        if doc.internal_id
        else extract_internal_id(content_for_hints)["value"]
    )

    hints = {
        "az_court": az_hint,
        "internal_id": internal_id_hint,
        # sender intentionally omitted: the regex-extracted sender is frequently
        # the email address (info@haidlfunk.de) or the court Eingangsstempel,
        # neither of which is the letterhead organization. Feeding it back biases
        # the AI toward emitting that value instead of reading the letterhead.
        # Mirrors the rationale for omitting `originator` and `issued_date`.
        "email_subject": batch_subject if batch_sender_email else None,
    }
    hints = {k: v for k, v in hints.items() if v is not None}

    prompt = ""

    # Inject known-party identity context so the AI can resolve originator_type
    # correctly even when the email sender domain doesn't match the document author.
    from app.services.case_service import get_case_opposing_parties
    from app.services.user_settings_service import get_party_identity

    party_identity = get_party_identity(db) if db is not None else {}
    case_opposing = (
        get_case_opposing_parties(doc.case_id, db)
        if db is not None and doc.case_id
        else []
    )
    party_block = format_party_context(
        own_self=party_identity.get("own_self", ""),
        own_parties=party_identity.get("own_parties", []),
        opposing_parties=case_opposing,
    )
    if party_block:
        prompt += party_block + "\n\n"

    if hints:
        prompt += f"### Heuristic Hints (found by regex, please verify):\n{json.dumps(hints, indent=2)}\n\n"
    prompt += f"### Document Content Preview:\n{fence(content_preview, 'document')}"

    try:
        result = call_json_ai(
            system_prompt=PHASE1_METADATA_SYSTEM,
            user_prompt=prompt,
            options=STAGE_OPTIONS["metadata"],
            debug_label=f"doc_{doc.id}_sync",
            schema=Phase1Metadata,
            model=model or cfg.summary_model,
            db=db,
            ingest_batch_id=doc.ingest_batch_id,
            case_id=doc.case_id,
            two_pass=True,
            # Per-doc stage: suppress the case-narrative preamble (Issue #5).
            include_user_context=False,
            base_url=base_url,
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
                model=model or cfg.summary_model,
                db=db,
                ingest_batch_id=doc.ingest_batch_id,
                case_id=doc.case_id,
                suppress_thinking=True,
                two_pass=True,
                include_user_context=False,
                base_url=base_url,
            )
        else:
            raise
    logger.debug(f"AI response parsed for '{doc.title}'")
    return result.model_dump()


def _summarize_document_sync(doc_id: int, db: Session) -> Document | None:
    """Synchronous wrapper for fire-and-forget background summary generation.

    Returns None only when doc_id doesn't resolve to a row (e.g. deleted
    between task dispatch and execution). When the doc exists but has no
    content or failed conversion, the (unmodified) Document is still
    returned — AI summarization is just skipped.
    """
    doc = db.query(Document).filter(Document.id == doc_id).first()
    if not doc or not doc.content or doc.content.startswith("Conversion failed:"):
        return doc

    # Reset to original filename before each metadata run so that AI-enriched
    # titles from prior runs don't anchor the next extraction pass.
    if doc.original_filename:
        from app.services.ingestion.service import extract_clean_title

        doc.title = extract_clean_title(doc.original_filename, doc.content)

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
