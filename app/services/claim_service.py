"""Phase 6 — Truth Map read side and user status lifecycle."""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Literal

from sqlalchemy.orm import Session, joinedload

from app.models.database import Claim, ClaimEvidence, Document, UserReaction
from app.models.enums import ClaimStatus
from app.repositories.claim import ClaimRepository
from app.repositories.user_reaction import UserReactionRepository

TruthMapFilter = Literal["open", "established", "refuted", "all"]

_FILTER_STATUSES: dict[TruthMapFilter, list[ClaimStatus]] = {
    "open": [ClaimStatus.CONTESTED, ClaimStatus.ASSERTED],
    "established": [ClaimStatus.ESTABLISHED],
    "refuted": [ClaimStatus.REFUTED],
    "all": [
        ClaimStatus.CONTESTED,
        ClaimStatus.ASSERTED,
        ClaimStatus.ESTABLISHED,
        ClaimStatus.REFUTED,
    ],
}

# Display order: most urgent first
_GROUP_ORDER = [
    ClaimStatus.CONTESTED,
    ClaimStatus.ASSERTED,
    ClaimStatus.ESTABLISHED,
    ClaimStatus.REFUTED,
]

# Transitions the user is allowed to request
_USER_ALLOWED: dict[ClaimStatus, set[ClaimStatus]] = {
    ClaimStatus.ASSERTED: {ClaimStatus.ESTABLISHED},
    ClaimStatus.CONTESTED: {ClaimStatus.ESTABLISHED},
    ClaimStatus.ESTABLISHED: {ClaimStatus.ASSERTED},
    ClaimStatus.REFUTED: {ClaimStatus.ASSERTED},
}


@dataclass
class EvidenceRow:
    evidence: ClaimEvidence
    document: Document
    reactions: list[UserReaction] = field(default_factory=list)


@dataclass
class ClaimRow:
    claim: Claim
    evidence: list[EvidenceRow] = field(default_factory=list)


@dataclass
class ClaimGroup:
    status: ClaimStatus
    claims: list[ClaimRow] = field(default_factory=list)


@dataclass
class TruthMapView:
    case_id: str
    filter: TruthMapFilter
    groups: list[ClaimGroup] = field(default_factory=list)
    open_claim_count: int = 0


class ClaimService:
    def __init__(self, db: Session):
        self._db = db
        self._claim_repo = ClaimRepository(db)
        self._reaction_repo = UserReactionRepository(db)

    def get_truth_map(
        self, case_id: str, filter_: TruthMapFilter = "open"
    ) -> TruthMapView:
        target_statuses = _FILTER_STATUSES[filter_]

        claims = (
            self._db.query(Claim)
            .options(joinedload(Claim.evidence).joinedload(ClaimEvidence.document))
            .filter(
                Claim.case_id == case_id,
                Claim.status.in_(target_statuses),
            )
            .order_by(Claim.last_updated_at.desc())
            .all()
        )

        # Batch-load reactions for all evidence documents
        doc_ids = list({ev.document_id for claim in claims for ev in claim.evidence})
        reactions_by_doc: dict[int, list[UserReaction]] = {}
        for reaction in self._reaction_repo.get_by_document_ids(doc_ids):
            reactions_by_doc.setdefault(reaction.document_id, []).append(reaction)

        # Build rows grouped by status
        groups_by_status: dict[ClaimStatus, list[ClaimRow]] = {
            s: [] for s in target_statuses
        }
        for claim in claims:
            evidence_rows = sorted(
                [
                    EvidenceRow(
                        evidence=ev,
                        document=ev.document,
                        reactions=reactions_by_doc.get(ev.document_id, []),
                    )
                    for ev in claim.evidence
                ],
                key=lambda r: (r.document.received_date or r.document.created_at),
            )
            groups_by_status[claim.status].append(
                ClaimRow(claim=claim, evidence=evidence_rows)
            )

        # Order groups by _GROUP_ORDER, skip empty ones
        groups = [
            ClaimGroup(status=s, claims=groups_by_status[s])
            for s in _GROUP_ORDER
            if s in groups_by_status and groups_by_status[s]
        ]

        open_count = (
            self._db.query(Claim)
            .filter(
                Claim.case_id == case_id,
                Claim.status.in_([ClaimStatus.CONTESTED, ClaimStatus.ASSERTED]),
            )
            .count()
        )

        return TruthMapView(
            case_id=case_id,
            filter=filter_,
            groups=groups,
            open_claim_count=open_count,
        )

    def transition_status(self, claim_id: int, target: ClaimStatus) -> Claim:
        """User-initiated status transition. Only ESTABLISHED and ASSERTED (reopen) are user-owned."""
        claim = self._claim_repo.get(claim_id)
        if claim is None:
            raise ValueError(f"Claim {claim_id} not found")

        allowed = _USER_ALLOWED.get(claim.status, set())
        if target not in allowed:
            if target in (ClaimStatus.CONTESTED, ClaimStatus.REFUTED):
                raise ValueError(
                    f"AI-owned: status '{target}' can only be set by the AI pipeline"
                )
            raise ValueError(f"Cannot transition from '{claim.status}' to '{target}'")

        updated = self._claim_repo.update_status(claim_id, target)
        if updated is None:
            raise ValueError(f"Claim {claim_id} not found")
        return updated
