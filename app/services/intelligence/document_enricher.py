"""4a — Per-document AI enrichment: significance_tier, document_type, key_passages, cost_delta."""

import hashlib
import logging
from datetime import UTC, datetime

from sqlalchemy.orm import Session

from app.config import SessionLocal
from app.models.database import Document, IngestBatch
from app.models.enums import DocumentRole, DocumentType, SignificanceTier
from app.models.schemas import (
    AISummarySchema,
    CostDeltaSchema,
    KeyPassageSchema,
)
from app.services.ai_config import get_chat_config
from app.services.ai_summary import get_content_preview
from app.services.intelligence._ai_call import call_json_ai
from app.services.intelligence.ai_options import STAGE_OPTIONS
from app.services.intelligence.content_gate import is_content_ai_ready
from app.services.intelligence.prompts import DOCUMENT_ENRICHER_SYSTEM
from app.services.intelligence.reaction_context import format_reactions_for_document
from app.services.intelligence.schemas import DocumentEnrichment
from app.services.text_offsets import find_text_offsets

logger = logging.getLogger(__name__)

VALID_SIGNIFICANCE_TIERS = {e.value for e in SignificanceTier}
VALID_DOCUMENT_TYPES = {e.value for e in DocumentType}
VALID_PASSAGE_KINDS = {
    "ruling",
    "holding",
    "deadline",
    "finding",
    "concession",
    "neutral",
}
VALID_COST_DIRECTIONS = {"incoming", "outgoing", "ruling", "none"}

THREAD_OPEN_TYPES = {
    DocumentType.STATEMENT,
    DocumentType.MOTION,
    DocumentType.REPORT,
    DocumentType.CORRESPONDENCE,
}


def _call_enricher_sync(
    doc: Document,
    model: str = "",
    reactions_block: str = "",
    batch_detected_actions: list[dict] | None = None,
) -> dict:
    """Synchronous AI call to enrich a single document. No DB session held."""
    import json

    content_preview = get_content_preview(doc, 60000)

    batch_context = ""
    if doc.role == DocumentRole.COVER_LETTER:
        batch_context = (
            "\nBatch context: This document is flagged as a cover letter (Begleitschreiben/"
            "Schreiben). If it is a pure relay, title it as such. If it contains "
            "substantive primary content (like a Motion/Antrag) that just happens "
            "to have attachments, prioritize the substantive content for the title. "
            "Keep document_type as 'relay' and significance_tier as 'administrative' "
            "unless you are certain this is NOT a relay at all."
        )
    elif doc.role == DocumentRole.ENCLOSURE and doc.attributed_originator:
        batch_context = f"\nBatch context: This document was enclosed in a cover letter. True originator: {doc.attributed_originator}"

    dates_context = ""
    if doc.received_date:
        dates_context += f"\nReceived date: {doc.received_date.strftime('%Y-%m-%d')}"
    if doc.issued_date:
        dates_context += f"\nIssued date: {doc.issued_date.strftime('%Y-%m-%d')}"

    batch_actions_context = ""
    if batch_detected_actions:
        batch_actions_context = (
            "\n\nBatch-detected actions (from cross-document analysis of all documents "
            "in this email/delivery):\n"
            + json.dumps(batch_detected_actions, ensure_ascii=False, indent=2)
            + "\nInclude any of these in your action_items ONLY if THIS document "
            "directly establishes or orders the action (e.g. a Verfügung, Ladung, or "
            "court order — not a relay cover letter). Set supersedes_date as indicated."
        )

    prompt = (
        f"{batch_context}{dates_context}{reactions_block}"
        f"{batch_actions_context}\n\n{content_preview}"
    ).lstrip("\n")

    result = call_json_ai(
        system_prompt=DOCUMENT_ENRICHER_SYSTEM,
        user_prompt=prompt,
        options=STAGE_OPTIONS["enrich"],
        debug_label=f"doc_{doc.id}_enricher",
        schema=DocumentEnrichment,
        model=model or None,
        ingest_batch_id=doc.ingest_batch_id,
        case_id=doc.case_id,
        two_pass=True,
    )
    return result.model_dump()


def _repair_passage_offsets(doc: Document, passage_dict: dict) -> dict:
    """Locate the passage text inside ``doc.content`` and stamp offsets on it.

    See :func:`app.services.text_offsets.find_text_offsets` for the matching
    cascade. Sets ``start_offset/end_offset`` to ``None`` when all passes fail.
    """
    text = passage_dict.get("text", "")
    offsets = find_text_offsets(doc.content or "", text)
    if offsets:
        passage_dict["start_offset"] = offsets[0]
        passage_dict["end_offset"] = offsets[1]
    else:
        passage_dict["start_offset"] = None
        passage_dict["end_offset"] = None
        logger.debug(f"Doc {doc.id}: passage text not located, no offset")
    return passage_dict


def _apply_enrichment(doc: Document, result: dict, db=None) -> None:
    """Write AI enrichment results to the document (caller commits)."""

    # title — only overwrite when AI returns a clean, non-empty title
    ai_title = (result.get("title") or "").strip()
    if ai_title and len(ai_title) <= 255:
        doc.title = ai_title
        doc.extraction_confidence = {
            **(doc.extraction_confidence or {}),
            "title": "high",
        }

    # issued_date — parse ISO date from document content (skip if already set by METADATA)
    # Confidence is tracked in METADATA; ENRICH does not override it.
    if not doc.issued_date:
        issued_date_str = (result.get("issued_date") or "").strip()
        if issued_date_str:
            try:
                parsed = datetime.strptime(issued_date_str[:10], "%Y-%m-%d").replace(
                    tzinfo=UTC
                )
                doc.issued_date = parsed
            except (ValueError, TypeError):
                pass

    # significance_tier
    tier_raw = (result.get("significance_tier") or "").lower()
    if tier_raw in VALID_SIGNIFICANCE_TIERS:
        doc.significance_tier = SignificanceTier(tier_raw)

    # document_type
    dtype_raw = (result.get("document_type") or "").lower()
    if dtype_raw in VALID_DOCUMENT_TYPES:
        doc.document_type = DocumentType(dtype_raw)

    # thread_open — derived from document_type, not AI-set
    if doc.document_type in THREAD_OPEN_TYPES:
        doc.thread_open = True

    # key_passages — validate schema, stamp kind fallback, repair offsets
    passages = result.get("key_passages")
    if isinstance(passages, list):
        validated = []
        for p in passages:
            if isinstance(p, dict) and p.get("text"):
                try:
                    passage_dict = KeyPassageSchema(**p).model_dump()
                    kind_raw = (passage_dict.get("kind") or "").strip().lower()
                    passage_dict["kind"] = (
                        kind_raw if kind_raw in VALID_PASSAGE_KINDS else "neutral"
                    )
                    if not passage_dict.get("id"):
                        text = passage_dict["text"]
                        passage_dict["id"] = hashlib.sha1(
                            f"{text}|{passage_dict['kind']}".encode()
                        ).hexdigest()[:12]
                    passage_dict = _repair_passage_offsets(doc, passage_dict)
                    validated.append(passage_dict)
                except Exception as e:
                    logger.warning(f"Doc {doc.id}: invalid key_passage skipped: {e}")
        doc.key_passages = validated or None

    # cost_delta — validate and normalise the typed signal
    cost_delta = result.get("cost_delta")
    if isinstance(cost_delta, dict):
        try:
            direction = (cost_delta.get("direction") or "none").lower()
            if direction not in VALID_COST_DIRECTIONS:
                direction = "none"
            # Infer kind from direction when the AI response pre-dates the kind field
            kind = cost_delta.get("kind") or (
                "invoice_court"
                if direction in {"incoming", "outgoing"}
                else "cost_ruling"
            )
            validated_delta = CostDeltaSchema(
                kind=kind,
                amount=float(cost_delta["amount"])
                if cost_delta.get("amount") is not None
                else None,
                direction=direction,
                description=str(cost_delta.get("description", "") or ""),
                allocation=cost_delta.get("allocation"),
                vat_included=cost_delta.get("vat_included"),
                offsets_signal_id=cost_delta.get("offsets_signal_id"),
            )
            doc.cost_delta = validated_delta.model_dump()
            # Auto-materialise invoice/vorschuss signals into the ledger
            if db is not None and kind in {
                "invoice_lawyer",
                "invoice_court",
                "vorschuss_lawyer",
                "vorschuss_court",
            }:
                try:
                    from app.services.cost_service import ensure_ledger_row_for_signal

                    ensure_ledger_row_for_signal(doc, doc.cost_delta, db)
                except Exception as mat_err:
                    logger.warning(
                        f"Doc {doc.id}: ledger materialisation failed: {mat_err}"
                    )
        except Exception as e:
            logger.warning(f"Doc {doc.id}: invalid cost_delta skipped: {e}")

    # ai_summary — must use exact keys that templates expect
    mgmt = result.get("management_summary") or {}
    try:
        validated_summary = AISummarySchema(
            legal_significance=mgmt.get("legal_significance"),
            required_action=mgmt.get("required_action"),
            financial_impact=mgmt.get("financial_impact"),
        )
        doc.ai_summary = validated_summary.model_dump()
    except Exception as e:
        logger.warning(f"Doc {doc.id}: invalid ai_summary skipped: {e}")

    # Track strategy and character count for UI transparency
    content_len = len(doc.content or "")
    new_meta = dict(doc.meta or {})
    new_meta["ai_context_strategy"] = "windowed" if content_len > 60000 else "full"
    new_meta["ai_context_chars"] = len(get_content_preview(doc, 60000))
    doc.meta = new_meta

    doc.ai_summary_created_at = datetime.now(UTC)


def enrich(doc_id: int) -> None:
    """Run AI enrichment for a single document.

    Three-phase design to avoid holding a DB session open during the AI call
    (which takes 10–60 s and would otherwise cause SQLite write-lock contention
    when multiple workers race to commit their results simultaneously):
      1. Read phase  — fetch all needed data, close session.
      2. AI phase    — call the model with no session held.
      3. Write phase — open a fresh session, apply results, commit, close.
    """
    # --- Phase 1: read ---
    db: Session = SessionLocal()
    try:
        cfg = get_chat_config(db)
        from app.services.ai_provider import chat_provider

        chat_provider.reload_from_db(db)
        doc = db.query(Document).filter(Document.id == doc_id).first()
        if not doc:
            logger.warning(f"Doc {doc_id} not found for enrichment")
            return
        if not is_content_ai_ready(doc):
            logger.info(f"Doc {doc_id} has no usable content for enrichment, skipping")
            return
        # Pre-format reactions while session is still open.
        formatted_reactions = format_reactions_for_document(db, doc_id)
        reactions_block = f"\n\n{formatted_reactions}" if formatted_reactions else ""
        model = cfg.summary_model
        # Capture metadata needed for the write phase before detaching.
        case_id = doc.case_id
        proceeding_id = doc.proceeding_id
        issued_date = doc.issued_date
        # Load batch-level detected actions as hints for the enricher AI.
        batch_detected_actions: list[dict] = []
        if doc.ingest_batch_id:
            batch = (
                db.query(IngestBatch)
                .filter(IngestBatch.id == doc.ingest_batch_id)
                .first()
            )
            if batch and batch.detected_actions:
                batch_detected_actions = batch.detected_actions
    finally:
        db.close()

    # --- Phase 2: AI call (no session held) ---
    result = _call_enricher_sync(
        doc,
        model=model,
        reactions_block=reactions_block,
        batch_detected_actions=batch_detected_actions or None,
    )

    # --- Phase 3: write ---
    db = SessionLocal()
    try:
        doc = db.query(Document).filter(Document.id == doc_id).first()
        if not doc:
            logger.warning(f"Doc {doc_id} disappeared before write phase")
            return
        _apply_enrichment(doc, result, db=db)

        from app.services.intelligence.action_items import create_from_payload

        create_from_payload(
            case_id,
            doc_id,
            proceeding_id,
            result.get("action_items") or [],
            db,
            source_doc_date=issued_date,
        )

        db.commit()
        logger.info(f"Doc {doc_id} enriched successfully")
    finally:
        db.close()
