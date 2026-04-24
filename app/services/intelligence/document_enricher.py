"""4a — Per-document AI enrichment: significance_tier, document_type, key_passages, cost_delta."""

import hashlib
import logging
from datetime import UTC, datetime

from sqlalchemy.orm import Session

from app.config import SessionLocal
from app.models.database import Document
from app.models.enums import DocumentRole, DocumentType, SignificanceTier
from app.models.schemas import (
    AISummarySchema,
    CostDeltaSchema,
    KeyPassageSchema,
)
from app.services.ai_config import get_effective_config
from app.services.ai_summary import get_content_preview
from app.services.intelligence._ai_call import call_json_ai
from app.services.intelligence.ai_options import STAGE_OPTIONS
from app.services.intelligence.prompts import DOCUMENT_ENRICHER_SYSTEM

logger = logging.getLogger(__name__)

VALID_SIGNIFICANCE_TIERS = {e.value for e in SignificanceTier}
VALID_DOCUMENT_TYPES = {e.value for e in DocumentType}
VALID_COST_DIRECTIONS = {"incoming", "outgoing", "ruling", "none"}

THREAD_OPEN_TYPES = {
    DocumentType.STATEMENT,
    DocumentType.MOTION,
    DocumentType.REPORT,
    DocumentType.CORRESPONDENCE,
}


def _call_enricher_sync(doc: Document, model: str = "", db=None) -> dict:
    """Synchronous AI call to enrich a single document."""
    content_preview = get_content_preview(doc, 6000)

    batch_context = ""
    if doc.role == DocumentRole.ENCLOSURE and doc.attributed_originator:
        batch_context = f"\nBatch context: This document was enclosed in a cover letter. True originator: {doc.attributed_originator}"

    prompt = f"Document title: {doc.title}{batch_context}\n\n{content_preview}"

    return call_json_ai(
        system_prompt=DOCUMENT_ENRICHER_SYSTEM,
        user_prompt=prompt,
        options=STAGE_OPTIONS["enrich"],
        debug_label=f"doc_{doc.id}_enricher",
        model=model or None,
        db=db,
        ingest_batch_id=doc.ingest_batch_id,
    )


def _normalize_text(s: str) -> str:
    """Collapse whitespace and normalize curly quotes for fuzzy offset search."""
    import re as _re

    s = s.replace("‘", "'").replace("’", "'")
    s = s.replace("“", '"').replace("”", '"')
    return _re.sub(r"\s+", " ", s).strip()


def _repair_passage_offsets(doc: Document, passage_dict: dict) -> dict:
    """Validate AI-supplied offsets; repair via text search if missing or wrong.

    Falls back to normalized (whitespace/quote-collapsed) search when exact
    search finds no unique match.
    """
    text = passage_dict.get("text", "")
    start = passage_dict.get("start_offset")
    end = passage_dict.get("end_offset")
    content = doc.content or ""

    # Validate exact match at claimed position
    if (
        start is not None
        and end is not None
        and isinstance(start, int)
        and isinstance(end, int)
        and 0 <= start < end <= len(content)
        and content[start:end] == text
    ):
        return passage_dict  # offsets are correct

    # Repair pass 1: exact substring search for a unique occurrence
    idx = content.find(text)
    if idx != -1 and content.find(text, idx + 1) == -1:
        passage_dict["start_offset"] = idx
        passage_dict["end_offset"] = idx + len(text)
        return passage_dict

    # Repair pass 2: normalized search — strip/collapse whitespace and curly quotes
    norm_text = _normalize_text(text)
    norm_content = _normalize_text(content)
    norm_idx = norm_content.find(norm_text)
    if norm_idx != -1 and norm_content.find(norm_text, norm_idx + 1) == -1:
        # Map normalized index back to original content by character walk
        orig_idx = 0
        norm_walked = 0
        import re as _re

        for m in _re.finditer(r"\S+|\s+", content):
            token = _normalize_text(m.group())
            if norm_walked + len(token) > norm_idx:
                orig_idx = m.start()
                break
            norm_walked += len(token) + 1  # +1 for collapsed space
        passage_dict["start_offset"] = orig_idx
        passage_dict["end_offset"] = min(orig_idx + len(text), len(content))
        logger.debug(f"Doc {doc.id}: passage offset repaired via normalized search")
        return passage_dict

    passage_dict["start_offset"] = None
    passage_dict["end_offset"] = None
    if idx == -1:
        logger.debug(f"Doc {doc.id}: passage text not found in content, no offset")
    else:
        logger.debug(f"Doc {doc.id}: passage text appears multiple times, no offset")
    return passage_dict


def _apply_enrichment(doc: Document, result: dict) -> None:
    """Write AI enrichment results to the document (caller commits)."""

    # title — only overwrite when AI returns a clean, non-empty title
    ai_title = (result.get("title") or "").strip()
    if ai_title and len(ai_title) <= 255:
        doc.title = ai_title

    # issued_date — parse ISO date from document content (skip if already set by METADATA)
    if not doc.issued_date:
        issued_date_str = (result.get("issued_date") or "").strip()
        if issued_date_str:
            try:
                parsed = datetime.strptime(issued_date_str[:10], "%Y-%m-%d")
                doc.issued_date = parsed
            except (ValueError, TypeError):
                pass

    # Update extraction confidence for issued_date to high (AI confirmation)
    conf = doc.extraction_confidence or {}
    conf["issued_date"] = "high" if doc.issued_date else "low"
    doc.extraction_confidence = conf

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

    # key_passages — validate schema and repair offsets
    passages = result.get("key_passages")
    if isinstance(passages, list):
        validated = []
        for p in passages:
            if isinstance(p, dict) and p.get("text"):
                try:
                    passage_dict = KeyPassageSchema(**p).model_dump()
                    if not passage_dict.get("id"):
                        text = passage_dict["text"]
                        kind = (passage_dict.get("kind") or "neutral").lower()
                        passage_dict["id"] = hashlib.sha1(
                            f"{text}|{kind}".encode()
                        ).hexdigest()[:12]
                    passage_dict = _repair_passage_offsets(doc, passage_dict)
                    validated.append(passage_dict)
                except Exception as e:
                    logger.warning(f"Doc {doc.id}: invalid key_passage skipped: {e}")
        doc.key_passages = validated or None

    # cost_delta — validate direction
    cost_delta = result.get("cost_delta")
    if isinstance(cost_delta, dict) and cost_delta.get("amount") is not None:
        try:
            direction = (cost_delta.get("direction") or "none").lower()
            if direction not in VALID_COST_DIRECTIONS:
                direction = "none"

            validated_delta = CostDeltaSchema(
                amount=float(cost_delta["amount"]),
                direction=direction,
                description=str(cost_delta.get("description", "")),
            )
            doc.cost_delta = validated_delta.model_dump()
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

    doc.ai_summary_created_at = datetime.now(UTC)


def enrich(doc_id: int) -> None:
    """Run AI enrichment for a single document."""
    db: Session = SessionLocal()
    try:
        cfg = get_effective_config(db)
        doc = db.query(Document).filter(Document.id == doc_id).first()
        if not doc:
            logger.warning(f"Doc {doc_id} not found for enrichment")
            return

        if not doc.content or doc.content.startswith("Conversion failed:"):
            logger.info(f"Doc {doc_id} has no usable content, skipping enrichment")
            return

        result = _call_enricher_sync(doc, model=cfg.summary_model, db=db)
        _apply_enrichment(doc, result)
        db.commit()
        logger.info(f"Doc {doc_id} enriched successfully")
    finally:
        db.close()
