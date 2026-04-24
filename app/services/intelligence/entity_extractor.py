"""4d — Per-document entity extraction: PERSON, ORGANIZATION, COURT, LAW_FIRM, CITATION, FINANCIAL."""

import logging

from sqlalchemy.orm import Session

from app.config import SessionLocal
from app.models.database import Document, Entity
from app.models.enums import EntityType, SignificanceTier
from app.services.ai_config import get_effective_config
from app.services.ai_summary import get_content_preview
from app.services.intelligence._ai_call import call_json_ai
from app.services.intelligence.ai_options import STAGE_OPTIONS
from app.services.intelligence.prompts import ENTITY_EXTRACTOR_SYSTEM

logger = logging.getLogger(__name__)

ELIGIBLE_TIERS = {
    SignificanceTier.CRITICAL,
    SignificanceTier.SIGNIFICANT,
    SignificanceTier.INFORMATIONAL,
}
VALID_ENTITY_TYPES = {e.name for e in EntityType}  # SAEnum stores .name (uppercase)


def _call_entity_extractor_sync(doc: Document, model: str = "", db=None) -> dict:
    content_preview = get_content_preview(doc, 60000)

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

    return call_json_ai(
        system_prompt=ENTITY_EXTRACTOR_SYSTEM,
        user_prompt=prompt,
        options=STAGE_OPTIONS["entities"],
        debug_label=f"doc_{doc.id}_entities",
        model=model or None,
        db=db,
        ingest_batch_id=doc.ingest_batch_id,
    )


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

        result = _call_entity_extractor_sync(doc, model=cfg.summary_model, db=db)
        count = _save_entities(doc, result, db)
        logger.info(f"Doc {doc_id}: extracted {count} entities")

        return None
    finally:
        db.rollback()
        db.close()
