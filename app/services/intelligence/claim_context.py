"""Build a prompt-ready summary of the Truth Map (Claims) and key Entities
for a given case.

Claims and their evidence are the case's most structured, source-anchored
knowledge — every claim is tied to the document(s) that asserted, supported,
or contested it. This module formats that knowledge so any AI prompt
(case chat, case brief) can draw on it, without duplicating the query/eager-
load logic at each call site.
"""

from collections.abc import Sequence

from sqlalchemy.orm import Session, joinedload

from app.models.database import Claim, Entity
from app.models.enums import ClaimEvidenceRole, ClaimStatus, EntityType
from app.repositories.claim import ClaimRepository
from app.repositories.entity import EntityRepository
from app.services.intelligence.prompts import sanitize_oneline

_ENTITY_LABELS = {
    EntityType.PERSON: "People",
    EntityType.ORGANIZATION: "Organizations",
}


def format_claims_for_case(
    db: Session,
    case_id: str,
    statuses: Sequence[ClaimStatus] = (ClaimStatus.ASSERTED, ClaimStatus.CONTESTED),
    limit: int = 15,
) -> str:
    """Return a formatted Truth Map block for the case's asserted/contested claims.

    Returns an empty string when there are no matching claims (safe to skip
    from a prompt).
    """
    candidate_ids = [
        c.id
        for c in ClaimRepository(db).claims_for_case(case_id, statuses=list(statuses))
    ][:limit]
    if not candidate_ids:
        return ""

    # Single eager-loaded query so the support/contest counts below don't
    # trigger per-claim lazy loads.
    claims = (
        db.query(Claim)
        .options(joinedload(Claim.evidence))
        .filter(Claim.id.in_(candidate_ids))
        .order_by(Claim.last_updated_at.desc())
        .all()
    )

    lines = []
    for c in claims:
        supports = sum(1 for e in c.evidence if e.role == ClaimEvidenceRole.SUPPORTS)
        contests = sum(1 for e in c.evidence if e.role == ClaimEvidenceRole.CONTESTS)
        lines.append(
            f"  - [{c.status.value}] {sanitize_oneline(c.claim_text, 300)} "
            f"(Evidence: {supports} supports, {contests} contests)"
        )

    return "Contested or Asserted Claims (Truth Map):\n" + "\n".join(lines)


def format_entities_for_case(
    db: Session,
    case_id: str,
    types: Sequence[EntityType] = (EntityType.PERSON, EntityType.ORGANIZATION),
    per_type_limit: int = 8,
) -> str:
    """Return a formatted block of key entities (people, organizations, ...) for the case.

    Grouped by type since Entity has no salience/mention-count signal to
    rank on. Returns an empty string when there are no matching entities.
    """
    repo = EntityRepository(db)
    sections = []
    for entity_type in types:
        entities: Sequence[Entity] = sorted(
            repo.get_by_case_and_type(case_id, entity_type), key=lambda e: e.name
        )[:per_type_limit]
        if not entities:
            continue
        label = _ENTITY_LABELS.get(
            entity_type, entity_type.value.replace("_", " ").title()
        )
        names = ", ".join(sanitize_oneline(e.name, 100) for e in entities)
        sections.append(f"  {label}: {names}")

    if not sections:
        return ""

    return "Key entities:\n" + "\n".join(sections)
