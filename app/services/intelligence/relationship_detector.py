"""4b — Per-document relationship detection against prior docs in the same proceeding."""

import json
import logging
from datetime import datetime

import httpx
from sqlalchemy.orm import Session

from app.config import AI_SUMMARY_MODEL, DATA_DIR, SessionLocal
from app.models.database import Document, DocumentRelationship
from app.models.enums import RelationshipConfidence, RelationshipType, SignificanceTier
from app.services.ai_provider import ai_provider
from app.services.intelligence._json import parse_json_response
from app.services.intelligence.prompts import RELATIONSHIP_DETECTOR_SYSTEM

logger = logging.getLogger(__name__)

CANDIDATE_TIERS = {SignificanceTier.CRITICAL, SignificanceTier.SIGNIFICANT}
VALID_RELATIONSHIP_TYPES = {e.value for e in RelationshipType}
MAX_CANDIDATES = 15


def _get_prior_docs(doc: Document, db: Session) -> list[Document]:
    """Return up to MAX_CANDIDATES prior docs in the same proceeding."""
    if not doc.proceeding_id:
        return []

    return (
        db.query(Document)
        .filter(
            Document.proceeding_id == doc.proceeding_id,
            Document.id != doc.id,
            Document.significance_tier.in_(list(CANDIDATE_TIERS)),
        )
        .order_by(Document.received_date.desc().nullslast())
        .limit(MAX_CANDIDATES)
        .all()
    )


def _build_candidate_summary(candidate: Document) -> str:
    first_passage = ""
    if candidate.key_passages and isinstance(candidate.key_passages, list):
        first_passage = candidate.key_passages[0].get("text", "")[:200]

    mgmt = candidate.ai_summary or {}
    sig = mgmt.get("legal_significance", "")[:150]

    return (
        f"ID={candidate.id} | {candidate.title} | "
        f"Date={candidate.received_date.date() if candidate.received_date else 'unknown'} | "
        f"Author={candidate.attributed_originator or candidate.sender or 'unknown'} | "
        f"Summary={sig} | Key passage: {first_passage}"
    )


def _call_relationship_detector_sync(
    doc: Document,
    candidates: list[Document],
    debug_file: str,
) -> dict:
    mgmt = doc.ai_summary or {}
    first_passage = ""
    if doc.key_passages and isinstance(doc.key_passages, list):
        first_passage = doc.key_passages[0].get("text", "")[:200]

    candidate_text = "\n".join(
        f"{i + 1}. {_build_candidate_summary(c)}" for i, c in enumerate(candidates)
    )
    prompt = (
        f"NEW DOCUMENT:\n"
        f"Title: {doc.title}\n"
        f"Summary: {mgmt.get('legal_significance', '')}\n"
        f"Key passage: {first_passage}\n\n"
        f"CANDIDATE PRIOR DOCUMENTS (use only these IDs):\n{candidate_text}"
    )

    import asyncio

    params = asyncio.run(
        ai_provider.get_generate_params(
            model=AI_SUMMARY_MODEL,
            prompt=prompt,
            system_prompt=RELATIONSHIP_DETECTOR_SYSTEM,
            stream=True,
            options={"num_ctx": 8192, "temperature": 0.1},
        )
    )
    ptype = asyncio.run(ai_provider.get_type())

    full_response = ""
    with httpx.Client(timeout=httpx.Timeout(120.0, read=60.0)) as client:
        with open(debug_file, "a") as f:
            f.write(f"--- RELATIONSHIP DETECTOR doc_id={doc.id} ---\n")
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
        raise ValueError(
            f"Relationship detector returned empty response for doc {doc.id}"
        )

    return parse_json_response(full_response)


def detect(doc_id: int) -> None:
    """Detect relationships from doc_id to prior documents in the same proceeding."""
    db: Session = SessionLocal()
    try:
        doc = db.query(Document).filter(Document.id == doc_id).first()
        if not doc:
            logger.warning(f"Doc {doc_id} not found for relationship detection")
            return

        if doc.significance_tier not in CANDIDATE_TIERS:
            logger.info(
                f"Doc {doc_id} has tier {doc.significance_tier}, skipping relationship detection"
            )
            return

        candidates = _get_prior_docs(doc, db)
        if not candidates:
            logger.info(
                f"Doc {doc_id} has no prior candidates in proceeding {doc.proceeding_id}"
            )
            return

        valid_candidate_ids = {c.id for c in candidates}

        debug_dir = DATA_DIR / "ai_debug"
        debug_dir.mkdir(parents=True, exist_ok=True)
        debug_file = str(
            debug_dir
            / f"doc_{doc_id}_{int(datetime.now().timestamp())}_relationships.log"
        )

        try:
            result = _call_relationship_detector_sync(doc, candidates, debug_file)
            relationships = result.get("relationships") or []

            for rel in relationships:
                to_id = rel.get("to_document_id")
                rel_type_raw = (rel.get("relationship_type") or "").lower()

                if to_id not in valid_candidate_ids:
                    logger.info(
                        f"Doc {doc_id}: relationship to ID {to_id} not in candidates, dropping"
                    )
                    continue

                if rel_type_raw not in VALID_RELATIONSHIP_TYPES:
                    logger.info(
                        f"Doc {doc_id}: invalid relationship_type '{rel_type_raw}', dropping"
                    )
                    continue

                existing = (
                    db.query(DocumentRelationship)
                    .filter(
                        DocumentRelationship.from_document_id == doc_id,
                        DocumentRelationship.to_document_id == to_id,
                        DocumentRelationship.relationship_type
                        == RelationshipType(rel_type_raw),
                    )
                    .first()
                )
                if existing:
                    continue

                notes = f"AI confidence: {rel.get('confidence', 'unknown')}. {rel.get('notes', '')}"
                db.add(
                    DocumentRelationship(
                        from_document_id=doc_id,
                        to_document_id=to_id,
                        relationship_type=RelationshipType(rel_type_raw),
                        confidence=RelationshipConfidence.AI_DETECTED,
                        notes=notes[:500],
                        created_at=datetime.now(),
                    )
                )

            db.commit()
            logger.info(
                f"Doc {doc_id}: relationship detection complete, {len(relationships)} proposed"
            )
        except Exception as e:
            logger.error(
                f"Doc {doc_id} relationship detection failed: {e}", exc_info=True
            )
    finally:
        db.close()
