"""4d — Per-document entity extraction: PERSON, ORGANIZATION, COURT, LAW_FIRM, CITATION, FINANCIAL."""

import json
import logging
from datetime import UTC, datetime

import httpx
from sqlalchemy.orm import Session

from app.config import DATA_DIR, SessionLocal
from app.core.async_utils import run_async
from app.models.database import Document, Entity
from app.models.enums import EntityType, SignificanceTier
from app.services.ai_config import get_effective_config
from app.services.ai_provider import ai_provider
from app.services.ai_summary import get_content_preview
from app.services.intelligence._json import parse_json_response
from app.services.intelligence.prompts import ENTITY_EXTRACTOR_SYSTEM

logger = logging.getLogger(__name__)

ELIGIBLE_TIERS = {
    SignificanceTier.CRITICAL,
    SignificanceTier.SIGNIFICANT,
    SignificanceTier.INFORMATIONAL,
}
VALID_ENTITY_TYPES = {e.name for e in EntityType}  # SAEnum stores .name (uppercase)


def _call_entity_extractor_sync(
    doc: Document, debug_file: str, model: str = ""
) -> dict:
    content_preview = get_content_preview(doc, 6000)

    mgmt = doc.ai_summary or {}
    legal_sig = mgmt.get("legal_significance", "")

    key_passages_text = ""
    if doc.key_passages and isinstance(doc.key_passages, list):
        excerpts = [
            p.get("text", "")[:200] for p in doc.key_passages[:3] if p.get("text")
        ]
        if excerpts:
            key_passages_text = "\n".join(f"- {e}" for e in excerpts)

    prompt = f"DOCUMENT TITLE: {doc.title}\nLEGAL SUMMARY: {legal_sig}\n"
    if key_passages_text:
        prompt += f"KEY PASSAGES:\n{key_passages_text}\n"
    prompt += f"\nCONTENT:\n{content_preview}"

    params = run_async(
        ai_provider.get_generate_params(
            model=model or get_effective_config().summary_model,
            prompt=prompt,
            system_prompt=ENTITY_EXTRACTOR_SYSTEM,
            stream=True,
            options={
                "num_ctx": 8192,
                "temperature": 0.1,
                "num_predict": 1500,
                "max_tokens": 1500,
            },
        )
    )

    ptype = run_async(ai_provider.get_type())
    full_response = ""

    with httpx.Client(timeout=httpx.Timeout(120.0, read=60.0)) as client:
        with open(debug_file, "a") as f:
            f.write(f"--- ENTITY EXTRACTOR doc_id={doc.id} ---\n")
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
        raise ValueError(f"Entity extractor returned empty response for doc {doc.id}")

    return parse_json_response(full_response)


def _save_entities(doc: Document, result: dict, db: Session) -> int:
    """Write extracted entities to DB. Returns count of new entities saved."""
    entities_raw = result.get("entities")
    if not isinstance(entities_raw, list):
        return 0

    count = 0
    for item in entities_raw:
        if not isinstance(item, dict):
            continue

        type_raw = (item.get("type") or "").upper()
        name = (item.get("name") or "").strip()

        if not name or type_raw not in VALID_ENTITY_TYPES:
            continue

        entity_type = EntityType[type_raw]  # Look up by NAME (uppercase)

        # Dedup: skip if same case+type+name already exists
        existing = (
            db.query(Entity)
            .filter(
                Entity.case_id == doc.case_id,
                Entity.type == entity_type,
                Entity.name == name,
            )
            .first()
        )
        if existing:
            continue

        context = (item.get("context_quote") or "")[:500]

        db.add(
            Entity(
                case_id=doc.case_id,
                type=entity_type,
                name=name,
                source_document_id=doc.id,
                extra_data={"context_quote": context} if context else None,
            )
        )
        count += 1

    if count:
        db.commit()

    return count


def extract(doc_id: int) -> str | None:
    """Extract named entities from doc_id.

    Returns a non-empty skip reason if skipped, or None if it ran.
    """
    db: Session = SessionLocal()
    try:
        cfg = get_effective_config(db)
        ai_provider.reload_from_db(db)
        doc = db.query(Document).filter(Document.id == doc_id).first()

        if not doc:
            logger.warning(f"Doc {doc_id} not found for entity extraction")
            return "document not found"

        if not doc.case_id or doc.case_id == "_TRIAGE":
            reason = "document not assigned to a case"
            logger.info(f"Doc {doc_id}: {reason}, skipping entity extraction")
            return reason

        if doc.significance_tier not in ELIGIBLE_TIERS:
            reason = f"significance_tier={doc.significance_tier} not eligible"
            logger.info(f"Doc {doc_id}: {reason}, skipping entity extraction")
            return reason

        if not doc.content or doc.content.startswith("Conversion failed:"):
            return "no usable content"

        debug_dir = DATA_DIR / "ai_debug"
        debug_dir.mkdir(parents=True, exist_ok=True)
        debug_file = str(
            debug_dir
            / f"doc_{doc_id}_{int(datetime.now(UTC).timestamp())}_entities.log"
        )

        try:
            result = _call_entity_extractor_sync(
                doc, debug_file, model=cfg.summary_model
            )
            count = _save_entities(doc, result, db)
            logger.info(f"Doc {doc_id}: extracted {count} entities")
        except Exception as e:
            logger.error(f"Doc {doc_id} entity extraction failed: {e}", exc_info=True)

        return None
    finally:
        db.rollback()
        db.close()
