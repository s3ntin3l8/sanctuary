"""Wave 2B: apply or dismiss AI-generated claim proposals.

Two flavors:

- `confirm_merge(proposal_id)` — collapses `new_claim` into `existing_claim`.
  All ClaimEvidence rows pointing at the new claim are repointed to the
  existing claim (deduped against same-document/same-role rows). The
  new claim is then deleted; its claim_vectors row goes with it.

- `confirm_evidence(proposal_id)` — writes the proposed ClaimEvidence
  row, then runs the same status-transition logic that used to live in
  the auto-apply path of the extractor (CONTESTS → CONTESTED if target
  was ASSERTED; REFUTES → REFUTED unless target was ESTABLISHED).

Dismissals just flip status to DISMISSED. Records are kept for audit /
analytics; not deleted.
"""

from __future__ import annotations

import logging
from datetime import UTC, datetime

from sqlalchemy import text
from sqlalchemy.orm import Session

from app.models.database import (
    Claim,
    ClaimEvidence,
    ClaimEvidenceProposal,
    ClaimMergeProposal,
)
from app.models.enums import (
    ClaimEvidenceRole,
    ClaimStatus,
    ProposalStatus,
)

logger = logging.getLogger(__name__)


def _now() -> datetime:
    return datetime.now(UTC).replace(tzinfo=None)


# ---------------------------------------------------------------------------
# Merge proposals
# ---------------------------------------------------------------------------


def confirm_merge(proposal_id: int, db: Session) -> ClaimMergeProposal | None:
    """Collapse `new_claim` into `existing_claim`. Idempotent at the
    proposal-status level (re-running on a CONFIRMED proposal is a no-op).
    """
    prop = db.get(ClaimMergeProposal, proposal_id)
    if prop is None or prop.status != ProposalStatus.PENDING:
        return prop

    new_claim = db.get(Claim, prop.new_claim_id)
    existing = db.get(Claim, prop.existing_claim_id)
    if not new_claim or not existing:
        prop.status = ProposalStatus.DISMISSED
        prop.resolved_at = _now()
        db.flush()
        return prop

    # Repoint evidence rows. Drop duplicates (same doc + same role on the
    # existing claim already).
    existing_keys = {
        (ev.document_id, ev.role)
        for ev in db.query(ClaimEvidence)
        .filter(ClaimEvidence.claim_id == existing.id)
        .all()
    }
    new_evidence = (
        db.query(ClaimEvidence).filter(ClaimEvidence.claim_id == new_claim.id).all()
    )
    for ev in new_evidence:
        if (ev.document_id, ev.role) in existing_keys:
            db.delete(ev)
        else:
            ev.claim_id = existing.id
            existing_keys.add((ev.document_id, ev.role))

    # Drop the new claim's vector blob (cascade FK only on integer keys).
    db.execute(
        text("DELETE FROM claim_vectors WHERE claim_id = :cid"),
        {"cid": new_claim.id},
    )
    db.delete(new_claim)

    prop.status = ProposalStatus.CONFIRMED
    prop.resolved_at = _now()

    # Bump existing claim's last_updated_at so it surfaces in recent views.
    existing.last_updated_at = _now()

    db.flush()
    logger.info(
        "merge confirmed: claim %s absorbed into %s (proposal %s)",
        prop.new_claim_id,
        prop.existing_claim_id,
        prop.id,
    )
    return prop


def dismiss_merge(proposal_id: int, db: Session) -> ClaimMergeProposal | None:
    prop = db.get(ClaimMergeProposal, proposal_id)
    if prop is None or prop.status != ProposalStatus.PENDING:
        return prop
    prop.status = ProposalStatus.DISMISSED
    prop.resolved_at = _now()
    db.flush()
    return prop


# ---------------------------------------------------------------------------
# Evidence-link proposals
# ---------------------------------------------------------------------------


def confirm_evidence(proposal_id: int, db: Session) -> ClaimEvidenceProposal | None:
    """Apply a proposed evidence link. Writes the ClaimEvidence row and runs
    the conservative status-transition rules:

    - CONTESTS on an ASSERTED claim → CONTESTED
    - REFUTES on a non-ESTABLISHED claim → REFUTED
    - ESTABLISHED claims (court findings) are never auto-flipped
    """
    prop = db.get(ClaimEvidenceProposal, proposal_id)
    if prop is None or prop.status != ProposalStatus.PENDING:
        return prop

    target = db.get(Claim, prop.target_claim_id)
    if target is None:
        prop.status = ProposalStatus.DISMISSED
        prop.resolved_at = _now()
        db.flush()
        return prop

    # Skip if the same evidence row already exists (idempotency for
    # double-clicks / retries).
    existing = (
        db.query(ClaimEvidence)
        .filter(
            ClaimEvidence.claim_id == prop.target_claim_id,
            ClaimEvidence.document_id == prop.source_document_id,
            ClaimEvidence.role == prop.proposed_role,
        )
        .first()
    )
    if existing is None:
        db.add(
            ClaimEvidence(
                claim_id=prop.target_claim_id,
                document_id=prop.source_document_id,
                role=prop.proposed_role,
                excerpt=prop.excerpt,
            )
        )

    if target.status != ClaimStatus.ESTABLISHED:
        if (
            prop.proposed_role == ClaimEvidenceRole.CONTESTS
            and target.status == ClaimStatus.ASSERTED
        ):
            target.status = ClaimStatus.CONTESTED
            target.last_updated_at = _now()
        elif prop.proposed_role == ClaimEvidenceRole.REFUTES:
            target.status = ClaimStatus.REFUTED
            target.last_updated_at = _now()

    prop.status = ProposalStatus.CONFIRMED
    prop.resolved_at = _now()
    db.flush()
    logger.info(
        "evidence proposal confirmed: %s on claim %s (proposal %s)",
        prop.proposed_role.value,
        prop.target_claim_id,
        prop.id,
    )
    return prop


def dismiss_evidence(proposal_id: int, db: Session) -> ClaimEvidenceProposal | None:
    prop = db.get(ClaimEvidenceProposal, proposal_id)
    if prop is None or prop.status != ProposalStatus.PENDING:
        return prop
    prop.status = ProposalStatus.DISMISSED
    prop.resolved_at = _now()
    db.flush()
    return prop


# ---------------------------------------------------------------------------
# Bulk lookups for the UI
# ---------------------------------------------------------------------------


def pending_merge_proposals_for_claim(
    claim_id: int, db: Session
) -> list[ClaimMergeProposal]:
    """All pending merge proposals where this claim is the NEW side
    (i.e. would be absorbed). The existing-side perspective is rare in
    the UI; we'd add it later if needed."""
    return (
        db.query(ClaimMergeProposal)
        .filter(
            ClaimMergeProposal.new_claim_id == claim_id,
            ClaimMergeProposal.status == ProposalStatus.PENDING,
        )
        .order_by(ClaimMergeProposal.proposed_at)
        .all()
    )


def pending_evidence_proposals_for_claim(
    claim_id: int, db: Session
) -> list[ClaimEvidenceProposal]:
    return (
        db.query(ClaimEvidenceProposal)
        .filter(
            ClaimEvidenceProposal.target_claim_id == claim_id,
            ClaimEvidenceProposal.status == ProposalStatus.PENDING,
        )
        .order_by(ClaimEvidenceProposal.proposed_at)
        .all()
    )


def pending_evidence_proposals_for_document(
    document_id: int, db: Session
) -> list[ClaimEvidenceProposal]:
    return (
        db.query(ClaimEvidenceProposal)
        .filter(
            ClaimEvidenceProposal.source_document_id == document_id,
            ClaimEvidenceProposal.status == ProposalStatus.PENDING,
        )
        .order_by(ClaimEvidenceProposal.proposed_at)
        .all()
    )
