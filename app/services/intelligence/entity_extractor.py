"""4d — Per-document entity extraction: PERSON, ORGANIZATION, COURT, LAW_FIRM, CITATION, FINANCIAL."""

import logging
import re

from sqlalchemy.orm import Session

from app.core.db_locks import ENTITIES_NS, advisory_xact_lock
from app.models.database import Case, Document, Entity
from app.models.enums import EntityType, SignificanceTier
from app.services.ai_config import get_chat_config
from app.services.ai_summary import content_windows
from app.services.entity_email import attach_sender_email
from app.services.intelligence._ai_call import call_json_ai
from app.services.intelligence.ai_options import STAGE_OPTIONS
from app.services.intelligence.prompts import ENTITY_EXTRACTOR_SYSTEM
from app.services.intelligence.schemas import EntityList
from app.services.normalization import (
    build_party_canonical_map,
    normalize_entity_name,
)

logger = logging.getLogger(__name__)

ELIGIBLE_TIERS = {
    SignificanceTier.CRITICAL,
    SignificanceTier.SIGNIFICANT,
    SignificanceTier.INFORMATIONAL,
}
VALID_ENTITY_TYPES = {e.name for e in EntityType}  # SAEnum stores .name (uppercase)

# A name starting with these prefixes (case-insensitive) is unambiguously a
# German court. The entity extractor sometimes mislabels them as LAW_FIRM or
# ORGANIZATION — we override at save time. Cheaper and more reliable than
# prompt nudging.
_COURT_NAME_RE = re.compile(
    r"^\s*(Amtsgericht|Landgericht|Oberlandesgericht|Bundesgerichtshof|"
    r"Bundesverfassungsgericht|Verwaltungsgericht|Sozialgericht|"
    r"Arbeitsgericht|Finanzgericht)\b",
    re.IGNORECASE,
)

# A PERSON name containing one of these separators is structural noise — a
# case-title string ("Hansen, Björn /. Liu, Yingying", "Müller v. Schmidt")
# or a compound name-attempt smush ("Liu Yingying / Frau Liu",
# "Yingying Liu / Ying Yang Liu") — and must be dropped. People don't have
# slashes in their names.
_CASE_TITLE_SEPARATOR_RE = re.compile(
    r"\s\.?/\.?\s|\s+v\.?s?\.\s+|\s+gegen\s+",
    re.IGNORECASE,
)


def _call_entity_extractor_sync(doc: Document, model: str = "") -> dict:
    """AI calls only — no DB session held. A long document is read window by window."""
    mgmt = doc.ai_summary or {}
    legal_sig = mgmt.get("legal_significance", "")

    key_passages_text = ""
    if doc.key_passages and isinstance(doc.key_passages, list):
        excerpts = [
            p.get("text", "")[:200] for p in doc.key_passages[:3] if p.get("text")
        ]
        if excerpts:
            key_passages_text = "\n".join(f"- {e}" for e in excerpts)

    from app.services.intelligence.prompts import fence, sanitize_oneline

    entities: list[dict] = []
    seen: set[tuple[str, str]] = set()
    for window in content_windows(doc):
        prompt = (
            f"DOCUMENT TITLE: {sanitize_oneline(doc.title, 200)}\n"
            f"LEGAL SUMMARY: {fence(legal_sig, 'ai_extracted')}\n"
        )
        if key_passages_text:
            prompt += f"KEY PASSAGES:\n{fence(key_passages_text, 'ai_extracted')}\n"
        prompt += (
            f"\n{window.note('Extract the entities named in THIS part only.')}"
            f"CONTENT:\n{fence(window.text, 'document')}"
        )
        result = call_json_ai(
            system_prompt=ENTITY_EXTRACTOR_SYSTEM,
            user_prompt=prompt,
            options=STAGE_OPTIONS["entities"],
            debug_label=f"doc_{doc.id}_entities"
            + (f"_p{window.index}" if window.is_part else ""),
            schema=EntityList,
            model=model or None,
            ingest_batch_id=doc.ingest_batch_id,
            case_id=doc.case_id,
            two_pass=True,
            # Per-doc stage: suppress the case-narrative preamble (Issue #5).
            include_user_context=False,
        )
        for entity in result.model_dump()["entities"]:
            key = (str(entity.get("name", "")).lower().strip(), str(entity.get("type")))
            # The first part's record of an entity wins: later parts only add the
            # entities it did not name.
            if key not in seen:
                seen.add(key)
                entities.append(entity)
    return {"entities": entities}


def _coerce_entity_type(entity_type: EntityType, name: str) -> EntityType:
    """Override the AI's type assignment for unambiguous cases.

    Currently: a name starting with a German court prefix MUST be COURT, even
    if the model called it LAW_FIRM or ORGANIZATION (the doc 31 / entity 95
    bug pattern).
    """
    if _COURT_NAME_RE.match(name):
        return EntityType.COURT
    return entity_type


def _is_case_title_string(name: str) -> bool:
    """A PERSON whose name contains a case-title separator is structural
    corruption — the AI captured the Rubrum string instead of the parties.
    Drop it; the real parties get extracted on their own."""
    return bool(_CASE_TITLE_SEPARATOR_RE.search(name))


def _save_entities(doc: Document, result: dict, db: Session) -> int:
    """Write extracted entities to DB. Returns count of new entities saved."""
    entities_raw = result.get("entities")
    if not isinstance(entities_raw, list):
        return 0

    # Dedup here is application logic (normalised names, party snapping), so the
    # schema can't enforce it. Serialise concurrent writers on this case: the
    # lock is held until this transaction commits, so the existing-rows read
    # below always sees what an earlier writer inserted.
    advisory_xact_lock(db, ENTITIES_NS, str(doc.case_id))

    # Build the canonical set from existing rows (case-scoped) so variants
    # of the same name collapse to one row instead of stacking duplicates.
    existing_rows = (
        db.query(Entity.type, Entity.name).filter(Entity.case_id == doc.case_id).all()
    )
    existing_keys: set[tuple[EntityType, str]] = {
        (t, normalize_entity_name(n, t)) for t, n in existing_rows
    }

    # Case-party canonical-name map (Issue #6): pulls Case.parties to snap
    # extracted variants to one canonical form per known actor.
    case = (
        db.query(Case).filter(Case.id == doc.case_id).first() if doc.case_id else None
    )
    party_canonicals = build_party_canonical_map(case)

    # First pass: collect canonical names from this payload so sub-unit
    # collapse (e.g. "Landratsamt X, Amt Y" → "Landratsamt X") can fire
    # when the parent appears in the same batch as the sub-unit.
    payload_canonicals: set[str] = set()
    for item in entities_raw:
        if not isinstance(item, dict):
            continue
        type_raw = (item.get("type") or "").upper()
        name = (item.get("name") or "").strip()
        if not name or type_raw not in VALID_ENTITY_TYPES:
            continue
        et = EntityType[type_raw]
        canonical = normalize_entity_name(name, et)
        if canonical:
            payload_canonicals.add(canonical)

    count = 0
    for item in entities_raw:
        if not isinstance(item, dict):
            continue

        type_raw = (item.get("type") or "").upper()
        name = (item.get("name") or "").strip()

        if not name or type_raw not in VALID_ENTITY_TYPES:
            continue

        entity_type = EntityType[type_raw]  # Look up by NAME (uppercase)

        # Issue #6: hard-override court names that the AI mislabelled.
        coerced = _coerce_entity_type(entity_type, name)
        if coerced != entity_type:
            logger.debug(
                "Doc %s: overriding entity type for %r: %s → %s",
                doc.id,
                name,
                entity_type.name,
                coerced.name,
            )
            entity_type = coerced

        # Issue #6: drop case-title strings stored as PERSON entities.
        if entity_type == EntityType.PERSON and _is_case_title_string(name):
            logger.debug(
                "Doc %s: dropping case-title PERSON entity %r — structural noise",
                doc.id,
                name,
            )
            continue

        canonical = normalize_entity_name(
            name, entity_type, canonical_names=payload_canonicals
        )
        if not canonical:
            continue

        # Issue #6: snap variants to a known case party's canonical spelling
        # so the row stores "Liu Yingying" (not "Liu", "Ying Liu", "Liu, Y.").
        # Falls through harmlessly when no match — non-party entities keep
        # their original name.
        stored_name = party_canonicals.get(canonical, name)

        # Round 6: when a snap occurred, the dedup key must match what's
        # actually stored. Otherwise an existing "Liu Yingying" row (normalize
        # key "liu yingying") doesn't match an incoming "Yingying Liu"
        # (normalize key "yingying liu" — different key, same person after
        # the snap) and a duplicate row gets inserted. Re-normalize using the
        # snapped name so dedup aligns with persistence.
        if stored_name is not name:
            dedup_key = normalize_entity_name(
                stored_name, entity_type, canonical_names=payload_canonicals
            )
        else:
            dedup_key = canonical

        if (entity_type, dedup_key) in existing_keys:
            continue
        existing_keys.add((entity_type, dedup_key))

        context = (item.get("context_quote") or "")[:500]

        db.add(
            Entity(
                case_id=doc.case_id,
                type=entity_type,
                name=stored_name,
                source_document_id=doc.id,
                extra_data={"context_quote": context} if context else None,
            )
        )
        count += 1

    # The mail's From address belongs to the entity its display name names. The
    # session does not autoflush, so the entities added above must be written
    # before they can be found.
    db.flush()
    attach_sender_email(db, doc.id)

    # Always commit: it also releases the advisory lock taken above, even when
    # nothing new was inserted.
    db.commit()

    return count


def extract(doc_id: int) -> str | None:
    """Extract named entities from doc_id.

    Returns a non-empty skip reason if skipped, or None if it ran.
    Three-phase: read → close DB → AI call → write.
    """
    from app.config import SessionLocal

    # Phase 1: read + skip checks
    db: Session = SessionLocal()
    try:
        cfg = get_chat_config(db)
        from app.services.ai_provider import chat_provider

        chat_provider.reload_from_db(db)
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

        model = cfg.summary_model
        # doc attributes remain accessible after session closes (NullPool detach)
    finally:
        db.close()

    # Phase 2: AI call — no DB session held
    result = _call_entity_extractor_sync(doc, model=model)

    # Phase 3: write
    db = SessionLocal()
    try:
        count = _save_entities(doc, result, db)
        logger.info(f"Doc {doc_id}: extracted {count} entities")
        return None
    finally:
        db.close()
