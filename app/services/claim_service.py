"""Phase 6 — Truth Map read side and user status lifecycle."""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime
from typing import Literal

from sqlalchemy.orm import Session, joinedload

from app.models.database import (
    Claim,
    ClaimEvidence,
    ClaimEvidenceProposal,
    ClaimMergeProposal,
    Document,
    UserReaction,
)
from app.models.enums import (
    ClaimEvidenceRole,
    ClaimStatus,
    OriginatorType,
    ProposalStatus,
)
from app.repositories.claim import ClaimRepository
from app.repositories.user_reaction import UserReactionRepository

TruthMapFilter = Literal["open", "established", "refuted", "all"]

_FILTER_STATUSES: dict[TruthMapFilter, list[ClaimStatus]] = {
    "open": [ClaimStatus.CONTESTED, ClaimStatus.ASSERTED, ClaimStatus.NEEDS_PROOF],
    "established": [ClaimStatus.ESTABLISHED],
    "refuted": [ClaimStatus.REFUTED],
    "all": [
        ClaimStatus.CONTESTED,
        ClaimStatus.ASSERTED,
        ClaimStatus.NEEDS_PROOF,
        ClaimStatus.ESTABLISHED,
        ClaimStatus.REFUTED,
    ],
}

# Display order: most urgent first
_GROUP_ORDER = [
    ClaimStatus.CONTESTED,
    ClaimStatus.ASSERTED,
    ClaimStatus.NEEDS_PROOF,
    ClaimStatus.ESTABLISHED,
    ClaimStatus.REFUTED,
]

# Transitions the user is allowed to request
_USER_ALLOWED: dict[ClaimStatus, set[ClaimStatus]] = {
    ClaimStatus.ASSERTED: {
        ClaimStatus.ESTABLISHED,
        ClaimStatus.CONTESTED,
        ClaimStatus.NEEDS_PROOF,
    },
    ClaimStatus.CONTESTED: {
        ClaimStatus.ESTABLISHED,
        ClaimStatus.ASSERTED,
        ClaimStatus.NEEDS_PROOF,
    },
    ClaimStatus.NEEDS_PROOF: {
        ClaimStatus.ESTABLISHED,
        ClaimStatus.ASSERTED,
        ClaimStatus.CONTESTED,
    },
    ClaimStatus.ESTABLISHED: {
        ClaimStatus.ASSERTED,
        ClaimStatus.CONTESTED,
        ClaimStatus.NEEDS_PROOF,
    },
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
class PendingMergeRow:
    """Wave 2C: a pending merge proposal scoped to a case, hydrated for UI
    rendering with both claim texts."""

    proposal_id: int
    confidence: str
    rationale: str | None
    new_claim_id: int
    new_claim_text: str
    existing_claim_id: int
    existing_claim_text: str


@dataclass
class PendingEvidenceRow:
    """A pending ClaimEvidenceProposal scoped to a case, hydrated for the
    "Pending cross-doc stances" block on the Truth Map. Mirrors the
    per-document HUD pill but surfaces it at case level so REFUTES
    proposals don't sit unconfirmed forever."""

    proposal_id: int
    proposed_role: ClaimEvidenceRole
    excerpt: str | None
    target_claim_id: int
    target_claim_text: str
    target_claim_status: ClaimStatus
    source_document_id: int
    source_document_title: str | None
    source_document_originator: OriginatorType | None
    source_document_case_id: str | None


@dataclass
class TruthMapView:
    case_id: str
    filter: TruthMapFilter
    groups: list[ClaimGroup] = field(default_factory=list)
    open_claim_count: int = 0
    pending_merges: list[PendingMergeRow] = field(default_factory=list)
    pending_evidence: list[PendingEvidenceRow] = field(default_factory=list)


class ClaimService:
    def __init__(self, db: Session):
        self._db = db
        self._claim_repo = ClaimRepository(db)
        self._reaction_repo = UserReactionRepository(db)

    def get_truth_map(
        self, case_id: str, filter_: TruthMapFilter = "open"
    ) -> TruthMapView:
        target_statuses = _FILTER_STATUSES[filter_]

        # Wave 2A: claims are global. Scope to a case via the
        # ClaimEvidence → Document → Document.case_id join.
        claims = list(
            self._claim_repo.claims_for_case(case_id, statuses=target_statuses)
        )

        # Eager-load evidence + documents for the rendering loop below.
        if claims:
            self._db.query(Claim).options(
                joinedload(Claim.evidence).joinedload(ClaimEvidence.document)
            ).filter(Claim.id.in_([c.id for c in claims])).all()

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
                key=lambda r: (r.document.issued_date or r.document.ingest_date),
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

        open_count = len(
            self._claim_repo.claims_for_case(
                case_id, statuses=[ClaimStatus.CONTESTED, ClaimStatus.ASSERTED]
            )
        )

        # Wave 2C: pending merge proposals visible from this case's
        # perspective. A proposal is "in this case" if either side has
        # evidence in this case — typically both sides do, but we accept
        # cross-case overlap for the rendering.
        pending_merges = self._load_pending_merges_for_case(case_id)

        # Pending cross-doc evidence proposals (the path to REFUTED status).
        # Surfaced here at case level so confirmed REFUTES proposals actually
        # land — they used to be reachable only via the per-document HUD.
        pending_evidence = self._load_pending_evidence_for_case(case_id)

        return TruthMapView(
            case_id=case_id,
            filter=filter_,
            groups=groups,
            open_claim_count=open_count,
            pending_merges=pending_merges,
            pending_evidence=pending_evidence,
        )

    def _load_pending_merges_for_case(self, case_id: str) -> list[PendingMergeRow]:
        """Find ClaimMergeProposal rows where at least one side has
        ClaimEvidence in `case_id`. Hydrates both claim texts for the UI."""
        rows = (
            self._db.query(ClaimMergeProposal)
            .filter(ClaimMergeProposal.status == ProposalStatus.PENDING)
            .order_by(ClaimMergeProposal.proposed_at.desc())
            .all()
        )
        if not rows:
            return []

        # Filter to proposals where at least one side has evidence in this case.
        relevant_claim_ids: set[int] = set()
        for r in rows:
            relevant_claim_ids.add(r.new_claim_id)
            relevant_claim_ids.add(r.existing_claim_id)
        if not relevant_claim_ids:
            return []

        in_case_claim_ids = {
            cid
            for (cid,) in self._db.query(Claim.id)
            .join(ClaimEvidence, ClaimEvidence.claim_id == Claim.id)
            .join(Document, Document.id == ClaimEvidence.document_id)
            .filter(
                Claim.id.in_(relevant_claim_ids),
                Document.case_id == case_id,
            )
            .distinct()
            .all()
        }

        claims_by_id = {
            c.id: c
            for c in self._db.query(Claim)
            .filter(Claim.id.in_(relevant_claim_ids))
            .all()
        }

        out: list[PendingMergeRow] = []
        for r in rows:
            if (
                r.new_claim_id not in in_case_claim_ids
                and r.existing_claim_id not in in_case_claim_ids
            ):
                continue
            new_claim = claims_by_id.get(r.new_claim_id)
            existing_claim = claims_by_id.get(r.existing_claim_id)
            if not new_claim or not existing_claim:
                continue
            out.append(
                PendingMergeRow(
                    proposal_id=r.id,
                    confidence=r.confidence.value,
                    rationale=r.rationale,
                    new_claim_id=r.new_claim_id,
                    new_claim_text=new_claim.claim_text,
                    existing_claim_id=r.existing_claim_id,
                    existing_claim_text=existing_claim.claim_text,
                )
            )
        return out

    def _load_pending_evidence_for_case(self, case_id: str) -> list[PendingEvidenceRow]:
        """Find PENDING ClaimEvidenceProposal rows touching this case — either
        the source document is in this case OR the target claim has evidence
        in this case. Skips proposals targeting dismissed claims."""
        rows = (
            self._db.query(ClaimEvidenceProposal)
            .filter(ClaimEvidenceProposal.status == ProposalStatus.PENDING)
            .order_by(ClaimEvidenceProposal.proposed_at.desc())
            .all()
        )
        if not rows:
            return []

        target_claim_ids = {r.target_claim_id for r in rows}
        source_doc_ids = {r.source_document_id for r in rows}

        # Source-doc-in-this-case set.
        in_case_doc_ids = {
            did
            for (did,) in self._db.query(Document.id)
            .filter(
                Document.id.in_(source_doc_ids),
                Document.case_id == case_id,
            )
            .all()
        }

        # Target-claim-has-evidence-in-this-case set.
        in_case_target_ids = {
            cid
            for (cid,) in self._db.query(Claim.id)
            .join(ClaimEvidence, ClaimEvidence.claim_id == Claim.id)
            .join(Document, Document.id == ClaimEvidence.document_id)
            .filter(
                Claim.id.in_(target_claim_ids),
                Document.case_id == case_id,
            )
            .distinct()
            .all()
        }

        claims_by_id = {
            c.id: c
            for c in self._db.query(Claim)
            .filter(
                Claim.id.in_(target_claim_ids),
                Claim.dismissed_at.is_(None),
            )
            .all()
        }
        docs_by_id = {
            d.id: d
            for d in self._db.query(Document)
            .filter(Document.id.in_(source_doc_ids))
            .all()
        }

        out: list[PendingEvidenceRow] = []
        for r in rows:
            target = claims_by_id.get(r.target_claim_id)
            if target is None:
                continue  # target was dismissed or deleted
            doc = docs_by_id.get(r.source_document_id)
            if (
                r.source_document_id not in in_case_doc_ids
                and r.target_claim_id not in in_case_target_ids
            ):
                continue
            out.append(
                PendingEvidenceRow(
                    proposal_id=r.id,
                    proposed_role=r.proposed_role,
                    excerpt=r.excerpt,
                    target_claim_id=r.target_claim_id,
                    target_claim_text=target.claim_text,
                    target_claim_status=target.status,
                    source_document_id=r.source_document_id,
                    source_document_title=doc.title if doc else None,
                    source_document_originator=doc.originator_type if doc else None,
                    source_document_case_id=doc.case_id if doc else None,
                )
            )
        return out

    def transition_status(self, claim_id: int, target: ClaimStatus) -> Claim:
        """User-initiated status transition. Only ESTABLISHED and ASSERTED (reopen) are user-owned."""
        claim = self._claim_repo.get(claim_id)
        if claim is None:
            raise ValueError(f"Claim {claim_id} not found")

        allowed = _USER_ALLOWED.get(claim.status, set())
        if target not in allowed:
            if target == ClaimStatus.REFUTED:
                raise ValueError(
                    "AI-owned: status 'refuted' can only be set by the AI pipeline"
                )
            raise ValueError(f"Cannot transition from '{claim.status}' to '{target}'")

        updated = self._claim_repo.update_status(claim_id, target)
        if updated is None:
            raise ValueError(f"Claim {claim_id} not found")
        return updated

    def dismiss_claim(self, claim_id: int) -> Claim:
        """Soft-delete a claim. Hides it from the Truth Map, the HUD, and the
        extractor's candidate-claims context. Pending evidence proposals
        targeting this claim are auto-dismissed so the new Pending Stances
        block on the Truth Map doesn't surface them with no live target."""
        claim = self._claim_repo.get(claim_id)
        if claim is None:
            raise ValueError(f"Claim {claim_id} not found")

        now = datetime.now()
        claim.dismissed_at = now
        self._db.query(ClaimEvidenceProposal).filter(
            ClaimEvidenceProposal.target_claim_id == claim_id,
            ClaimEvidenceProposal.status == ProposalStatus.PENDING,
        ).update(
            {"status": ProposalStatus.DISMISSED, "resolved_at": now},
            synchronize_session=False,
        )
        self._db.flush()
        return claim
