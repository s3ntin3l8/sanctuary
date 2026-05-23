from collections.abc import Sequence
from datetime import datetime

from sqlalchemy import distinct, func, not_
from sqlalchemy.orm import Session, aliased

from app.models.database import Claim, ClaimEvidence, Document
from app.models.enums import ClaimEvidenceRole, ClaimStatus, ClaimType
from app.repositories.base import BaseRepository


class ClaimRepository(BaseRepository[Claim]):
    """Repository for the global Claim table.

    Wave 2A: claims have no case_id / proceeding_id / source_document_id of
    their own. Case scope comes from joining through ClaimEvidence to
    Document, where the document's case_id is authoritative. Helpers in
    this repo centralize that join so call sites don't reinvent it.
    """

    def __init__(self, db: Session):
        super().__init__(Claim, db)

    # -----------------------------------------------------------------
    # Case-scoped lookups (now via ClaimEvidence → Document join)
    # -----------------------------------------------------------------

    def get_by_case_count(self, case_ids: list[str]) -> dict[str, int]:
        """Bulk count claims by case IDs.

        A claim counts toward case X if it has any ClaimEvidence row whose
        document is in case X. Cross-case claims (same claim, evidence in
        multiple cases) count once per case they touch.
        """
        if not case_ids:
            return {}
        results = (
            self.db.query(Document.case_id, func.count(distinct(Claim.id)))
            .join(ClaimEvidence, ClaimEvidence.claim_id == Claim.id)
            .join(Document, Document.id == ClaimEvidence.document_id)
            .filter(Document.case_id.in_(case_ids))
            .group_by(Document.case_id)
            .all()
        )
        return dict(results)

    def claims_for_case(
        self, case_id: str, statuses: list[ClaimStatus] | None = None
    ) -> Sequence[Claim]:
        """All non-dismissed claims with at least one piece of evidence rooted in `case_id`."""
        q = (
            self.db.query(Claim)
            .join(ClaimEvidence, ClaimEvidence.claim_id == Claim.id)
            .join(Document, Document.id == ClaimEvidence.document_id)
            .filter(Document.case_id == case_id, Claim.dismissed_at.is_(None))
            .distinct()
        )
        if statuses:
            q = q.filter(Claim.status.in_(statuses))
        return q.order_by(Claim.last_updated_at.desc()).all()

    def claims_for_document(self, document_id: int) -> Sequence[Claim]:
        """All non-dismissed claims with any evidence (any role) anchored on `document_id`."""
        return (
            self.db.query(Claim)
            .join(ClaimEvidence, ClaimEvidence.claim_id == Claim.id)
            .filter(
                ClaimEvidence.document_id == document_id,
                Claim.dismissed_at.is_(None),
            )
            .distinct()
            .order_by(Claim.last_updated_at.desc())
            .all()
        )

    def claims_asserted_by_document(self, document_id: int) -> Sequence[Claim]:
        """Non-dismissed claims this document originally ASSERTED (one row per claim).

        Replaces the old `Claim.source_document_id == doc.id` filter — the
        ASSERTS evidence row is the canonical "originated by" link.
        """
        return (
            self.db.query(Claim)
            .join(ClaimEvidence, ClaimEvidence.claim_id == Claim.id)
            .filter(
                ClaimEvidence.document_id == document_id,
                ClaimEvidence.role == ClaimEvidenceRole.ASSERTS,
                Claim.dismissed_at.is_(None),
            )
            .order_by(Claim.id)
            .all()
        )

    def claims_only_originated_by_document(self, document_id: int) -> Sequence[Claim]:
        """Non-dismissed claims this document originated and no other doc has touched.

        Returns claims where (a) this doc has an ASSERTS evidence row, AND (b)
        no ClaimEvidence row from any other document exists. These claims have
        no cross-doc signal — their entire existence (text, status, evidence)
        comes from this document, so they can be safely cleaned on
        re-enrichment of the source. Claims that any other document has
        SUPPORTED, CONTESTED, REFUTED, or CITED_AS_PROOF are excluded — those
        carry independent signal we must preserve.
        """
        # Alias the inner ClaimEvidence so SQLAlchemy doesn't try to auto-
        # correlate the outer ClaimEvidence (used for the ASSERTS join) into
        # the subquery's FROM list — that auto-correlation strips both
        # references and leaves the EXISTS subquery without any FROM.
        ce_inner = aliased(ClaimEvidence)
        other_doc_evidence = (
            self.db.query(ce_inner.id)
            .filter(
                ce_inner.claim_id == Claim.id,
                ce_inner.document_id != document_id,
            )
            .exists()
        )
        return (
            self.db.query(Claim)
            .join(ClaimEvidence, ClaimEvidence.claim_id == Claim.id)
            .filter(
                ClaimEvidence.document_id == document_id,
                ClaimEvidence.role == ClaimEvidenceRole.ASSERTS,
                Claim.dismissed_at.is_(None),
                not_(other_doc_evidence),
            )
            .order_by(Claim.id)
            .all()
        )

    def get_open_in_case(self, case_id: str, limit: int = 20) -> Sequence[Claim]:
        """Open, non-dismissed claims (ASSERTED/CONTESTED) with evidence in this case.

        Used by the extractor to feed candidate-claims context to the AI.
        Dismissed claims are deliberately excluded — once a user says "this
        isn't a claim", we must not feed it back to the LLM as a candidate
        for cross-document evidence linking.
        """
        return (
            self.db.query(Claim)
            .join(ClaimEvidence, ClaimEvidence.claim_id == Claim.id)
            .join(Document, Document.id == ClaimEvidence.document_id)
            .filter(
                Document.case_id == case_id,
                Claim.status.in_([ClaimStatus.ASSERTED, ClaimStatus.CONTESTED]),
                Claim.dismissed_at.is_(None),
            )
            .distinct()
            .order_by(Claim.last_updated_at.desc())
            .limit(limit)
            .all()
        )

    # -----------------------------------------------------------------
    # Mutation
    # -----------------------------------------------------------------

    def create_claim(
        self,
        claim_text: str,
        claim_type: ClaimType,
        status: ClaimStatus = ClaimStatus.ASSERTED,
        is_precedent: bool = False,
    ) -> Claim:
        """Create a global claim row. The caller is responsible for adding
        a `ClaimEvidence(role=ASSERTS, document_id=…)` row immediately
        after — that's the canonical "originated by" link and the only way
        to scope this claim to a case."""
        return self.create(
            claim_text=claim_text,
            claim_type=claim_type,
            status=status,
            is_precedent=is_precedent,
            first_made_at=datetime.now(),
            last_updated_at=datetime.now(),
        )

    def update_status(self, claim_id: int, status: ClaimStatus) -> Claim | None:
        return self.update(claim_id, status=status, last_updated_at=datetime.now())
