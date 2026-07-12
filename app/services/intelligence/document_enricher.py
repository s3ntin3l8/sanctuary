"""4a — Per-document AI enrichment: significance_tier, document_type, key_passages, cost_delta."""

import hashlib
import logging
from datetime import UTC, datetime
from typing import cast

from sqlalchemy.orm import Session

from app.config import SessionLocal
from app.models.database import Document, IngestBatch
from app.models.enums import DocumentRole, DocumentType, SignificanceTier
from app.models.schemas import (
    AISummarySchema,
    CostDeltaKind,
    CostDeltaSchema,
    KeyPassageSchema,
)
from app.services.ai_config import get_chat_config
from app.services.ai_summary import get_content_preview
from app.services.intelligence._ai_call import call_json_ai
from app.services.intelligence._party_context import format_party_context
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

# Placeholder values the AI occasionally emits when it has no real answer.
# Any management_summary field matching these is treated as missing.
_PLACEHOLDER_VALUES = frozenset({"...", "…", "tbd", "n/a", "none", ""})


def _is_placeholder(s: str | None) -> bool:
    """Return True if `s` is a known AI placeholder rather than real content."""
    return not s or s.strip().lower() in _PLACEHOLDER_VALUES or len(s.strip()) < 4


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
    party_context: str = "",
    base_url: str | None = None,
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
            + "\nFor each batch-detected action with confidence: high and a due_date, "
            "include it in your action_items ONLY when THIS document is the most-direct "
            "source — defined as: (1) a Verfügung/Ladung/court order setting the date, OR "
            "(2) when no order document exists in the batch, the cover letter announcing it. "
            "Do NOT include the action if another document in the batch is a more-direct "
            "source (the dedup constraint on (case_id, due_date, action_type) prevents "
            "double-creation). For confidence: low actions, apply the strict "
            "'directly establishes' filter. Set supersedes_date as indicated.\n"
            "Set the `addressee` for every entry — including batch-detected items — to "
            "the party the action targets (user|opposing|third_party|court). Do not "
            "auto-promote a third-party or opposing-directed obligation to addressee=user."
        )

    party_block = (party_context + "\n\n") if party_context else ""

    from app.services.intelligence.prompts import fence

    prompt = (
        f"{party_block}{batch_context}{dates_context}{reactions_block}"
        f"{batch_actions_context}\n\n{fence(content_preview, 'document')}"
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
        # Per-doc stage: suppress the case-narrative preamble. The enricher was
        # the worst offender — doc 44 (ICBC bank cert about Teilungsversteigerung)
        # got `required_action="File as supporting evidence in custody
        # proceedings"` because the preamble framed everything as custody.
        include_user_context=False,
        base_url=base_url,
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
    from app.services.intelligence._court_identity import reconcile_ai_fields

    # Resolve self-contradictions in the AI output before writing any fields.
    # In particular: court_relay=true requires a court sender; MOTION/STATEMENT
    # doc types cannot have originator=court. Mutates `result` in place.
    reconcile_ai_fields(doc, result)

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

    # court_relay — set BEFORE cost_delta + action_items so downstream gates
    # (Streitwert source check, action-item court gate) see the AI's current
    # value, not the prior-run value loaded from the DB.
    court_relay_raw = result.get("court_relay")
    if isinstance(court_relay_raw, bool):
        doc.court_relay = court_relay_raw

    # Proactive stale-Streitwert cleanup — runs regardless of whether the
    # AI emits a cost_delta this run. Handles the re-enrichment case where
    # the AI correctly emits no Streitwert but a prior run's bad row needs
    # to vanish (e.g. doc #98 from ib-0033 after originator flipped to OPPOSING).
    if db is not None:
        try:
            from app.services.cost_service import purge_disqualified_streitwert

            purge_disqualified_streitwert(doc, db)
        except Exception as purge_err:
            logger.warning(
                f"Doc {doc.id}: proactive Streitwert purge failed: {purge_err}"
            )

    # cost_delta — validate and normalise the typed signal
    cost_delta = result.get("cost_delta")
    if isinstance(cost_delta, dict):
        try:
            direction = (cost_delta.get("direction") or "none").lower()
            if direction not in VALID_COST_DIRECTIONS:
                direction = "none"
            # Infer kind from direction when the AI response pre-dates the kind field.
            # Not whitelisted here (unlike direction above) because CostDeltaSchema's
            # Pydantic validation already rejects any value outside CostDeltaKind —
            # an invalid AI-emitted kind raises, and is caught by the except below.
            kind = cast(
                CostDeltaKind,
                cost_delta.get("kind")
                or (
                    "invoice_court"
                    if direction in {"incoming", "outgoing"}
                    else "cost_ruling"
                ),
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
            # Route the validated signal to its destination: LegalCost for
            # invoice/vorschuss kinds, CostSignal for streitwert/cost_ruling/pkh.
            # No persistent JSON column — the signal is materialised once.
            if db is not None:
                try:
                    from app.services.cost_service import materialize_cost_signal

                    materialize_cost_signal(doc, validated_delta.model_dump(), db)
                except Exception as mat_err:
                    logger.warning(
                        f"Doc {doc.id}: cost signal materialisation failed: {mat_err}"
                    )
        except Exception as e:
            logger.warning(f"Doc {doc.id}: invalid cost_delta skipped: {e}")

    # ai_summary — must use exact keys that templates expect.
    # Reject all-placeholder responses (AI returned "..." for every field) so
    # the doc stays with ai_summary=NULL and can be re-enriched rather than
    # permanently storing useless placeholder text.
    mgmt = result.get("management_summary") or {}
    legal_sig = mgmt.get("legal_significance")
    req_action = mgmt.get("required_action")
    fin_impact = mgmt.get("financial_impact")

    if all(_is_placeholder(v) for v in (legal_sig, req_action, fin_impact)):
        logger.warning(
            "Doc %d: AI returned all-placeholder management_summary — leaving null",
            doc.id,
        )
    else:
        try:
            validated_summary = AISummarySchema(
                legal_significance=None if _is_placeholder(legal_sig) else legal_sig,
                required_action=None if _is_placeholder(req_action) else req_action,
                financial_impact=None if _is_placeholder(fin_impact) else fin_impact,
            )
            doc.ai_summary = validated_summary.model_dump()
            doc.ai_summary_created_at = datetime.now(UTC)
        except Exception as e:
            logger.warning("Doc %d: invalid ai_summary skipped: %s", doc.id, e)

    # Track strategy and character count for UI transparency
    content_len = len(doc.content or "")
    new_meta = dict(doc.meta or {})
    new_meta["ai_context_strategy"] = "windowed" if content_len > 60000 else "full"
    new_meta["ai_context_chars"] = len(get_content_preview(doc, 60000))
    doc.meta = new_meta

    # (court_relay was already applied above — moved earlier so the
    # Streitwert and action-item gates see the AI's current value.)


def enrich(doc_id: int) -> None:
    """Run AI enrichment for a single document.

    Three-phase design to avoid holding a DB session open during the AI call
    (which takes 10–60 s and would otherwise hold row locks against other
    workers racing to commit their results against the same rows):
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
        # Load party identity so the enricher can resolve originator roles and
        # determine court_relay without a hint-feedback loop.
        from app.services.case_service import get_case_opposing_parties
        from app.services.user_settings_service import get_party_identity

        party_identity = get_party_identity(db)
        # get_case_opposing_parties requires a str case_id; a doc not yet
        # assigned to a case (case_id is None) has no case-level opposing
        # parties to look up — skip the call (it would return [] anyway).
        case_opposing = (
            get_case_opposing_parties(doc.case_id, db) if doc.case_id else []
        )
        party_context = format_party_context(
            own_self=party_identity.get("own_self", ""),
            own_parties=party_identity.get("own_parties", []),
            opposing_parties=case_opposing,
        )
    finally:
        db.close()

    # --- Phase 2: AI call (no session held) ---
    result = _call_enricher_sync(
        doc,
        model=model,
        reactions_block=reactions_block,
        batch_detected_actions=batch_detected_actions or None,
        party_context=party_context,
    )

    # --- Phase 3: write ---
    db = SessionLocal()
    try:
        doc = db.query(Document).filter(Document.id == doc_id).first()
        if not doc:
            logger.warning(f"Doc {doc_id} disappeared before write phase")
            return
        _apply_enrichment(doc, result, db=db)

        from app.models.enums import OriginatorType
        from app.services.intelligence.action_items import (
            create_from_payload,
            purge_action_items_from_doc,
        )

        # Action item court gate (mirrors the Streitwert gate, same rationale):
        # only direct court documents authoritatively set Termine/Fristen.
        # Opposing party letters and court relays carrying party submissions
        # may quote court-set deadlines but the source isn't authoritative,
        # so we don't persist their action items. On rejection, also erase
        # any stale non-superseded items from prior runs (tombstones stay).
        is_court_source = (
            doc.originator_type == OriginatorType.COURT and not doc.court_relay
        )
        if is_court_source:
            # create_from_payload no-ops for a falsy case_id anyway (docs not
            # yet assigned to a case have nothing to attach action items to);
            # guard here so the call site matches its str (non-Optional) signature.
            if case_id:
                create_from_payload(
                    case_id,
                    doc_id,
                    proceeding_id,
                    result.get("action_items") or [],
                    db,
                    source_doc_date=issued_date,
                )
        else:
            purged = purge_action_items_from_doc(doc_id, db)
            if purged:
                logger.info(
                    "Doc %s: purged %d stale action item(s) — non-court "
                    "source (originator=%s, court_relay=%s)",
                    doc_id,
                    purged,
                    doc.originator_type,
                    doc.court_relay,
                )

        db.commit()
        logger.info(f"Doc {doc_id} enriched successfully")
    finally:
        db.close()
