"""4a — Per-document AI enrichment: significance_tier, document_type, key_passages, cost_delta."""

import json
import logging
from datetime import UTC, datetime

import httpx
from sqlalchemy.orm import Session

from app.config import AI_SUMMARY_MODEL, DATA_DIR, SessionLocal
from app.models.database import Document
from app.models.enums import DocumentRole, DocumentType, SignificanceTier
from app.services.ai_provider import ai_provider
from app.services.ai_summary import get_content_preview
from app.services.intelligence._json import parse_json_response
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


def _call_enricher_sync(doc: Document, debug_file: str) -> dict:
    """Synchronous AI call to enrich a single document."""
    content_preview = get_content_preview(doc, 6000)

    batch_context = ""
    if doc.role == DocumentRole.ENCLOSURE and doc.attributed_originator:
        batch_context = f"\nBatch context: This document was enclosed in a cover letter. True originator: {doc.attributed_originator}"

    prompt = f"Document title: {doc.title}{batch_context}\n\n{content_preview}"

    import asyncio

    params = asyncio.run(
        ai_provider.get_generate_params(
            model=AI_SUMMARY_MODEL,
            prompt=prompt,
            system_prompt=DOCUMENT_ENRICHER_SYSTEM,
            stream=True,
            options={"num_ctx": 16384, "temperature": 0.2},
        )
    )
    ptype = asyncio.run(ai_provider.get_type())

    full_response = ""
    with httpx.Client(timeout=httpx.Timeout(120.0, read=60.0)) as client:
        with open(debug_file, "a") as f:
            f.write(f"--- ENRICHER doc_id={doc.id} ---\n")
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
        raise ValueError(f"Enricher returned empty response for doc {doc.id}")

    return parse_json_response(full_response)


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
                validated.append(
                    {
                        "text": str(p.get("text", "")),
                        "rationale": str(p.get("rationale", "")),
                        "span": str(p.get("span", "")),
                    }
                )
        doc.key_passages = validated or None

    # cost_delta — validate direction
    cost_delta = result.get("cost_delta")
    if isinstance(cost_delta, dict) and cost_delta.get("amount") is not None:
        direction = (cost_delta.get("direction") or "none").lower()
        if direction not in VALID_COST_DIRECTIONS:
            logger.info(
                f"Doc {doc.id}: cost_delta.direction '{direction}' invalid, normalizing to 'none'"
            )
            direction = "none"
        doc.cost_delta = {
            "amount": float(cost_delta["amount"]),
            "direction": direction,
            "description": str(cost_delta.get("description", "")),
        }

    # ai_summary — must use exact keys that templates expect
    mgmt = result.get("management_summary") or {}
    doc.ai_summary = {
        "legal_significance": mgmt.get("legal_significance"),
        "required_action": mgmt.get("required_action"),
        "financial_impact": mgmt.get("financial_impact"),
    }
    doc.ai_summary_status = "generated"
    doc.ai_summary_created_at = datetime.now(UTC)


def enrich(doc_id: int) -> None:
    """Run AI enrichment for a single document."""
    db: Session = SessionLocal()
    try:
        doc = db.query(Document).filter(Document.id == doc_id).first()
        if not doc:
            logger.warning(f"Doc {doc_id} not found for enrichment")
            return

        if not doc.content or doc.content.startswith("Conversion failed:"):
            logger.info(f"Doc {doc_id} has no usable content, skipping enrichment")
            return

        debug_dir = DATA_DIR / "ai_debug"
        debug_dir.mkdir(parents=True, exist_ok=True)
        debug_file = str(
            debug_dir / f"doc_{doc_id}_{int(datetime.now().timestamp())}_enricher.log"
        )

        try:
            result = _call_enricher_sync(doc, debug_file)
            _apply_enrichment(doc, result)
            logger.info(f"Doc {doc_id} enriched successfully")
        except Exception as e:
            logger.error(f"Doc {doc_id} enrichment failed: {e}", exc_info=True)
            doc.ai_summary_status = "failed"
            doc.ai_summary = {"error": str(e)}

        db.commit()
    finally:
        db.close()
