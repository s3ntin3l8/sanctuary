"""4d — Per-document entity extraction: PERSON, ORGANIZATION, COURT, LAW_FIRM, CITATION, FINANCIAL."""

import logging
import re

from sqlalchemy.orm import Session

from app.config import SessionLocal
from app.models.database import Case, Document, Entity
from app.models.enums import EntityType, SignificanceTier
from app.services.ai_config import get_chat_config
from app.services.ai_summary import get_content_preview
from app.services.intelligence._ai_call import call_json_ai
from app.services.intelligence.ai_options import STAGE_OPTIONS
from app.services.intelligence.prompts import ENTITY_EXTRACTOR_SYSTEM
from app.services.intelligence.schemas import EntityList
from app.services.normalization import normalize_entity_name

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
    """AI call only — no DB session held."""
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

    from app.services.intelligence.prompts import fence, sanitize_oneline

    prompt = (
        f"DOCUMENT TITLE: {sanitize_oneline(doc.title, 200)}\n"
        f"LEGAL SUMMARY: {fence(legal_sig, 'ai_extracted')}\n"
    )
    if key_passages_text:
        prompt += f"KEY PASSAGES:\n{fence(key_passages_text, 'ai_extracted')}\n"
    prompt += f"\nCONTENT:\n{fence(content_preview, 'document')}"

    result = call_json_ai(
        system_prompt=ENTITY_EXTRACTOR_SYSTEM,
        user_prompt=prompt,
        options=STAGE_OPTIONS["entities"],
        debug_label=f"doc_{doc.id}_entities",
        schema=EntityList,
        model=model or None,
        ingest_batch_id=doc.ingest_batch_id,
        case_id=doc.case_id,
        two_pass=True,
        # Per-doc stage: suppress the case-narrative preamble (Issue #5).
        include_user_context=False,
    )
    return result.model_dump()


def _build_party_canonical_map(case: Case | None) -> dict[str, str]:
    """Snap-to-canonical lookup for known case parties.

    Returns a dict mapping `normalize_entity_name(variant, PERSON|ORG)` to the
    canonical party name in `Case.parties`. The entity-extractor often emits
    PERSON variants like "Liu", "Yingying Liu", "J. Liu, Yingying" that share
    a dedup key with "Liu Yingying" once normalized — but some variants don't
    quite normalize identically (East-Asian vs Western name ordering doesn't
    collapse, initials, comma-reverse edge cases). Anchoring on the case's
    `parties` list pins variants of both orderings to one row.
    """
    if not case or not case.parties:
        return {}
    mapping: dict[str, str] = {}
    for party in case.parties:
        if not isinstance(party, dict):
            continue
        canonical = (party.get("name") or "").strip()
        if not canonical:
            continue
        # Map all entity-type normalisations so humans ("Liu Yingying") and
        # orgs ("Haidl Funk Rechtsanwälte") and courts ("Amtsgericht
        # Ingolstadt") all snap uniformly.
        for et in (
            EntityType.PERSON,
            EntityType.ORGANIZATION,
            EntityType.LAW_FIRM,
            EntityType.COURT,
        ):
            key = normalize_entity_name(canonical, et)
            if key:
                mapping[key] = canonical
        # For two-token PERSON names, expand the keyset so all common
        # variants of the same person map back to the canonical spelling:
        #
        #  - swap order ("Liu Yingying" ↔ "Yingying Liu") for East-Asian
        #    vs Western name ordering (existing).
        #  - initial-form ("Y. Liu", "Liu, Y.", "L. Yingying") for the
        #    abbreviated references the AI sometimes emits (Round 7 — fixes
        #    the post-R6 doc 25 "Liu, Y." duplicate).
        #
        # Single-token bare-surname references ("Liu", "Frau Liu" stripped
        # of honorific) are deliberately NOT mapped — a bare surname is
        # ambiguous between "Liu Yingying" and "Liu Jun" (or any other Liu
        # on the case) and snapping would falsely merge them. The
        # honorific-stripped "Frau Liu" → key "liu" case is documented as
        # accepted ambiguity; user can merge manually if needed.
        tokens = canonical.split()
        if len(tokens) == 2:
            first, last = tokens
            variants = [
                f"{last} {first}",  # swapped order
                f"{first[0]}. {last}",  # "Y. Liu"
                f"{last}, {first[0]}.",  # "Liu, Y."
                f"{last[0]}. {first}",  # "L. Yingying" (swapped + initial)
            ]
            for variant in variants:
                key = normalize_entity_name(variant, EntityType.PERSON)
                if key and key not in mapping:
                    mapping[key] = canonical
    return mapping


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
    party_canonicals = _build_party_canonical_map(case)

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

    if count:
        db.commit()

    return count


def extract(doc_id: int) -> str | None:
    """Extract named entities from doc_id.

    Returns a non-empty skip reason if skipped, or None if it ran.
    Three-phase: read → close DB → AI call → write.
    """
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
