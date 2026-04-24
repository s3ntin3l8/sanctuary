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


def _apply_enrichment(doc: Document, result: dict) -> None:
    """Write AI enrichment results to the document (caller commits)."""
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

    # key_passages
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
