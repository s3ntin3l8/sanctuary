"""Map the address a mail was sent from to the case entity it belongs to."""

from __future__ import annotations

from email.utils import parseaddr

from sqlalchemy.orm import Session

from app.models.database import Case, Document, Entity, IngestBatch, UserSettings
from app.models.enums import EntityType, OriginatorType
from app.services.normalization import normalize_entity_name

NAMED_ACTORS = (
    EntityType.PERSON,
    EntityType.LAW_FIRM,
    EntityType.COURT,
    EntityType.ORGANIZATION,
)


def attach_sender_email(db: Session, doc_id: int) -> int:
    """Record the From address on the case entity its display name names.

    Only fills an entity that has no address yet and never creates one: the
    header says who wrote from this address, the entity list says who exists.
    The user's own mail is skipped. Returns how many entities were filled.
    """
    doc = db.get(Document, doc_id)
    if (
        doc is None
        or not doc.case_id
        or doc.case_id == "_TRIAGE"
        or doc.ingest_batch_id is None
        or not doc.email_source_id
        or doc.originator_type == OriginatorType.OWN
    ):
        return 0
    batch = db.get(IngestBatch, doc.ingest_batch_id)
    if batch is None:
        return 0
    source = next(
        (s for s in (batch.email_sources or []) if s.get("id") == doc.email_source_id),
        None,
    )
    if source is None:
        return 0
    name, address = parseaddr(source.get("sender") or "")
    address = address.strip().lower()
    if not name.strip() or "@" not in address:
        return 0
    # The user's own mail is skipped by its From address, not by the document's
    # originator: an own forward or an own mail's attachments are not stamped OWN.
    if address in _own_addresses(db, batch.owner_id):
        return 0
    from app.services.intelligence.entity_extractor import _build_party_canonical_map

    case = db.get(Case, doc.case_id)
    snaps = _build_party_canonical_map(case)
    entities = (
        db.query(Entity)
        .filter(Entity.case_id == doc.case_id, Entity.type.in_(NAMED_ACTORS))
        .all()
    )
    filled = 0
    for entity in entities:
        if entity.email is not None:
            continue
        # The extractor's own keying: sub-units collapse into the parent that
        # exists in the case, and variants of a known party snap to its spelling.
        known = {
            normalize_entity_name(e.name, e.type)
            for e in entities
            if e.type == entity.type
        }
        wanted = _key(name, entity.type, known, snaps)
        # A bare honorific ("Dr.", "Frau") normalises to nothing: it names nobody.
        if wanted and wanted == _key(entity.name, entity.type, known, snaps):
            entity.email = address
            filled += 1
    return filled


def _key(
    name: str, entity_type: EntityType, known: set[str], snaps: dict[str, str]
) -> str:
    canonical = normalize_entity_name(name, entity_type, canonical_names=known)
    snapped = snaps.get(canonical, name)
    if snapped is name:
        return canonical
    return normalize_entity_name(snapped, entity_type, canonical_names=known)


def _own_addresses(db: Session, owner_id: int | None) -> set[str]:
    if owner_id is None:
        return set()
    row = db.query(UserSettings).filter(UserSettings.user_id == owner_id).first()
    stored = (row.settings_json or {}).get("gmail_own_addresses") if row else None
    return {a.strip().lower() for a in stored or [] if a and a.strip()}
