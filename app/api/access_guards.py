"""Per-object access guards for API routes.

Each guard is a FastAPI dependency factory: it resolves an ID path parameter
to its owning case (or, for _TRIAGE/no-case objects, its direct owner) and
checks the current user's access via app.services.access_service. Raises 404
(never 403) on denial — matching require_triage_object_owner's convention
(app/api/triage/ownership.py) — so a 404 never reveals whether the object
exists to a user who can't see it.

Every guard sets `_is_access_guard = True` on the dependency callable it
returns, so the route-coverage meta-test
(tests/unit/test_route_authorization_coverage.py) can find it in a route's
flattened dependency tree without needing to know each guard by name.

Untriaged documents (case_id is None or "_TRIAGE", the shared singleton
triage case) are not case-scoped — every user's untriaged docs share the
same case_id, so access_service.can_view_case would either let everyone see
everyone's triage queue or, if _TRIAGE's owner_id happens to be some other
user, block the real owner from their own docs. Ownership on the Document/
IngestBatch row itself is the only correct signal there, same as
require_triage_object_owner already does for the triage/slicing routers.
"""

from __future__ import annotations

from fastapi import Depends, HTTPException, Path
from sqlalchemy.orm import Session

from app.dependencies import get_current_user, get_db
from app.models.database import (
    ActionItem,
    Case,
    Claim,
    ClaimEvidence,
    ClaimEvidenceProposal,
    ClaimMergeProposal,
    Conversation,
    CostSignal,
    Document,
    DocumentPin,
    IngestBatch,
    LegalCost,
    Proceeding,
    User,
)
from app.services import access_service

_NO_CASE = (None, "_TRIAGE")


def _check_case_id(db: Session, user: User, case_id: str | None, *, edit: bool) -> bool:
    if not case_id or case_id in _NO_CASE:
        return False
    case = db.query(Case).filter(Case.id == case_id).first()
    if edit:
        return access_service.can_edit_case(db, user, case)
    return access_service.can_view_case(db, user, case)


def check_owned_or_case_access(
    db: Session, user: User, *, owner_id: int | None, case_id: str | None, edit: bool
) -> bool:
    if not case_id or case_id in _NO_CASE:
        return access_service.is_admin(user) or (
            owner_id is not None and owner_id == user.id
        )
    return _check_case_id(db, user, case_id, edit=edit)


def require_case_access(*, edit: bool = False):
    """Resolve `case_id` from the path; 404 unless the user can view/edit it."""

    async def _dep(
        case_id: str = Path(...),
        db: Session = Depends(get_db),
        user: User = Depends(get_current_user),
    ) -> Case:
        case = db.query(Case).filter(Case.id == case_id).first()
        allowed = (
            access_service.can_edit_case(db, user, case)
            if edit
            else access_service.can_view_case(db, user, case)
        )
        if not case or not allowed:
            raise HTTPException(status_code=404, detail="Not found")
        return case

    _dep._is_access_guard = True  # type: ignore[attr-defined]
    return _dep


def require_document_access(*, edit: bool = False):
    """Resolve `doc_id` from the path; 404 unless the user can view/edit its case
    (or, for an untriaged doc, unless they own it)."""

    async def _dep(
        doc_id: int = Path(...),
        db: Session = Depends(get_db),
        user: User = Depends(get_current_user),
    ) -> Document:
        doc = db.query(Document).filter(Document.id == doc_id).first()
        if doc is None or not check_owned_or_case_access(
            db, user, owner_id=doc.owner_id, case_id=doc.case_id, edit=edit
        ):
            raise HTTPException(status_code=404, detail="Not found")
        return doc

    _dep._is_access_guard = True  # type: ignore[attr-defined]
    return _dep


def require_batch_access(*, edit: bool = False):
    """Resolve `batch_id` from the path; same rules as require_document_access,
    applied to the batch's own case_id/owner_id."""

    async def _dep(
        batch_id: int = Path(...),
        db: Session = Depends(get_db),
        user: User = Depends(get_current_user),
    ) -> IngestBatch:
        batch = db.query(IngestBatch).filter(IngestBatch.id == batch_id).first()
        if batch is None or not check_owned_or_case_access(
            db, user, owner_id=batch.owner_id, case_id=batch.case_id, edit=edit
        ):
            raise HTTPException(status_code=404, detail="Not found")
        return batch

    _dep._is_access_guard = True  # type: ignore[attr-defined]
    return _dep


def require_proceeding_access(*, edit: bool = False):
    """Resolve `proceeding_id` from the path; 404 unless the user can view/edit
    the proceeding's case."""

    async def _dep(
        proceeding_id: int = Path(...),
        db: Session = Depends(get_db),
        user: User = Depends(get_current_user),
    ) -> Proceeding:
        proceeding = db.query(Proceeding).filter(Proceeding.id == proceeding_id).first()
        if proceeding is None or not _check_case_id(
            db, user, proceeding.case_id, edit=edit
        ):
            raise HTTPException(status_code=404, detail="Not found")
        return proceeding

    _dep._is_access_guard = True  # type: ignore[attr-defined]
    return _dep


def require_action_item_access(*, edit: bool = False):
    """Resolve `item_id` from the path; 404 unless the user can view/edit the
    action item's case."""

    async def _dep(
        item_id: int = Path(...),
        db: Session = Depends(get_db),
        user: User = Depends(get_current_user),
    ) -> ActionItem:
        item = db.query(ActionItem).filter(ActionItem.id == item_id).first()
        if item is None or not _check_case_id(db, user, item.case_id, edit=edit):
            raise HTTPException(status_code=404, detail="Not found")
        return item

    _dep._is_access_guard = True  # type: ignore[attr-defined]
    return _dep


def require_pin_access(*, edit: bool = False):
    """Resolve `pin_id` from the path; 404 unless the user can view/edit the
    pinned document's case (or owns the untriaged document)."""

    async def _dep(
        pin_id: int = Path(...),
        db: Session = Depends(get_db),
        user: User = Depends(get_current_user),
    ) -> DocumentPin:
        pin = db.query(DocumentPin).filter(DocumentPin.id == pin_id).first()
        if pin is None:
            raise HTTPException(status_code=404, detail="Not found")
        doc = db.query(Document).filter(Document.id == pin.document_id).first()
        if doc is None or not check_owned_or_case_access(
            db, user, owner_id=doc.owner_id, case_id=doc.case_id, edit=edit
        ):
            raise HTTPException(status_code=404, detail="Not found")
        return pin

    _dep._is_access_guard = True  # type: ignore[attr-defined]
    return _dep


def require_cost_access(*, edit: bool = False):
    """Resolve `cost_id` from the path; 404 unless the user can view/edit the
    cost's case."""

    async def _dep(
        cost_id: int = Path(...),
        db: Session = Depends(get_db),
        user: User = Depends(get_current_user),
    ) -> LegalCost:
        cost = db.query(LegalCost).filter(LegalCost.id == cost_id).first()
        if cost is None or not _check_case_id(db, user, cost.case_id, edit=edit):
            raise HTTPException(status_code=404, detail="Not found")
        return cost

    _dep._is_access_guard = True  # type: ignore[attr-defined]
    return _dep


def require_cost_signal_access(*, edit: bool = False):
    """Resolve `signal_id` (CostSignal) from the path; 404 unless the user
    can view/edit the signal's case."""

    async def _dep(
        signal_id: int = Path(...),
        db: Session = Depends(get_db),
        user: User = Depends(get_current_user),
    ) -> CostSignal:
        signal = db.query(CostSignal).filter(CostSignal.id == signal_id).first()
        if signal is None or not _check_case_id(db, user, signal.case_id, edit=edit):
            raise HTTPException(status_code=404, detail="Not found")
        return signal

    _dep._is_access_guard = True  # type: ignore[attr-defined]
    return _dep


def claim_linked_case_ids(db: Session, claim_id: int) -> set[str]:
    """Every distinct case_id reachable from a claim's evidence documents.

    Claims are global (see Claim's own docstring): the same claim can be
    evidence-linked from documents in different cases, potentially owned by
    different users. See #159 for the residual leak this implies (a user
    with access to only one of several linked cases can still tell that
    *some* other case references this claim, via edit-access denial without
    a matching view-access grant) — out of scope for this route-level
    gate to resolve fully.
    """
    rows = (
        db.query(Document.case_id)
        .join(ClaimEvidence, ClaimEvidence.document_id == Document.id)
        .filter(ClaimEvidence.claim_id == claim_id, Document.case_id.isnot(None))
        .distinct()
        .all()
    )
    return {row[0] for row in rows if row[0] and row[0] != "_TRIAGE"}


def claim_linked_owner_ids(db: Session, claim_id: int) -> set[int]:
    """Owner ids of every document that evidences this claim, regardless of
    case. Fallback for claims with no real-case evidence yet (e.g. sourced
    only from _TRIAGE documents), where claim_linked_case_ids is empty and
    ownership of the evidencing documents is the only available signal."""
    rows = (
        db.query(Document.owner_id)
        .join(ClaimEvidence, ClaimEvidence.document_id == Document.id)
        .filter(ClaimEvidence.claim_id == claim_id)
        .distinct()
        .all()
    )
    return {row[0] for row in rows if row[0] is not None}


def _check_case_ids(db: Session, user: User, case_ids: set[str], *, edit: bool) -> bool:
    """View = access to at least one case_id; edit = access to all of them
    (a mutation touching several cases' claims shouldn't be authorized by
    access to just one of them)."""
    if not case_ids:
        return False
    checks = [_check_case_id(db, user, cid, edit=edit) for cid in case_ids]
    return all(checks) if edit else any(checks)


def claim_access_allowed(db: Session, user: User, claim_id: int, *, edit: bool) -> bool:
    case_ids = claim_linked_case_ids(db, claim_id)
    if case_ids:
        return _check_case_ids(db, user, case_ids, edit=edit)
    owner_ids = claim_linked_owner_ids(db, claim_id)
    if not owner_ids:
        return False
    if access_service.is_admin(user):
        return True
    # Same view/edit asymmetry as _check_case_ids: view = owns at least one
    # evidencing (still-untriaged) document; edit = owns *all* of them, so a
    # claim evidenced from two different users' _TRIAGE documents can't be
    # edited by either owner unilaterally.
    return owner_ids == {user.id} if edit else user.id in owner_ids


def require_claim_access(*, edit: bool = False):
    """Resolve `claim_id` from the path.

    Claims can span multiple cases (see claim_linked_case_ids). Viewing
    requires access to at least one linked case (claims are meant to be
    cross-referenced); editing requires edit access to *every* linked case,
    since a mutation from one case's context could otherwise affect a claim
    another user relies on in a case they don't share. A claim with no
    real-case evidence yet (only _TRIAGE) falls back to ownership of its
    evidencing documents.
    """

    async def _dep(
        claim_id: int = Path(...),
        db: Session = Depends(get_db),
        user: User = Depends(get_current_user),
    ) -> Claim:
        claim = db.query(Claim).filter(Claim.id == claim_id).first()
        if claim is None or not claim_access_allowed(db, user, claim_id, edit=edit):
            raise HTTPException(status_code=404, detail="Not found")
        return claim

    _dep._is_access_guard = True  # type: ignore[attr-defined]
    return _dep


def merge_proposal_access_allowed(
    db: Session, user: User, proposal: ClaimMergeProposal, *, edit: bool
) -> bool:
    """Confirming a merge mutates both sides (new_claim's evidence moves onto
    existing_claim, new_claim is deleted), so edit requires edit access to
    every case linked to *either* claim. Shared by require_merge_proposal_access
    and the batch merge route (claims.py), which must apply the same rule
    per-proposal rather than trusting the URL's case_id alone."""
    new_allowed = claim_access_allowed(db, user, proposal.new_claim_id, edit=edit)
    existing_allowed = claim_access_allowed(
        db, user, proposal.existing_claim_id, edit=edit
    )
    return (
        (new_allowed and existing_allowed)
        if edit
        else (new_allowed or existing_allowed)
    )


def require_merge_proposal_access(*, edit: bool = False):
    """Resolve `proposal_id` (ClaimMergeProposal) from the path."""

    async def _dep(
        proposal_id: int = Path(...),
        db: Session = Depends(get_db),
        user: User = Depends(get_current_user),
    ) -> ClaimMergeProposal:
        proposal = (
            db.query(ClaimMergeProposal)
            .filter(ClaimMergeProposal.id == proposal_id)
            .first()
        )
        if proposal is None or not merge_proposal_access_allowed(
            db, user, proposal, edit=edit
        ):
            raise HTTPException(status_code=404, detail="Not found")
        return proposal

    _dep._is_access_guard = True  # type: ignore[attr-defined]
    return _dep


def require_evidence_proposal_access(*, edit: bool = False):
    """Resolve `proposal_id` (ClaimEvidenceProposal) from the path.

    Confirming writes a new ClaimEvidence row linking the source document to
    the target claim, so edit requires edit access to the document's case
    *and* every case linked to the target claim."""

    async def _dep(
        proposal_id: int = Path(...),
        db: Session = Depends(get_db),
        user: User = Depends(get_current_user),
    ) -> ClaimEvidenceProposal:
        proposal = (
            db.query(ClaimEvidenceProposal)
            .filter(ClaimEvidenceProposal.id == proposal_id)
            .first()
        )
        if proposal is None:
            raise HTTPException(status_code=404, detail="Not found")
        doc = (
            db.query(Document)
            .filter(Document.id == proposal.source_document_id)
            .first()
        )
        doc_allowed = doc is not None and check_owned_or_case_access(
            db, user, owner_id=doc.owner_id, case_id=doc.case_id, edit=edit
        )
        claim_allowed = claim_access_allowed(
            db, user, proposal.target_claim_id, edit=edit
        )
        allowed = (
            (doc_allowed and claim_allowed) if edit else (doc_allowed or claim_allowed)
        )
        if not allowed:
            raise HTTPException(status_code=404, detail="Not found")
        return proposal

    _dep._is_access_guard = True  # type: ignore[attr-defined]
    return _dep


def require_conversation_access():
    """Resolve `conversation_id` from the path; 404 unless the requester owns
    it AND still has view access to its scope (case/document) right now.
    Conversations are per-user (Conversation.user_id), not shared within a
    case — two users both viewing the same case each get their own chat
    history for it. The scope re-check matters because a CaseShare can be
    revoked after a conversation exists — ownership alone would let a user
    keep chatting (and retrieving passages) from a case they no longer have
    any access to."""

    async def _dep(
        conversation_id: int = Path(...),
        db: Session = Depends(get_db),
        user: User = Depends(get_current_user),
    ) -> Conversation:
        conv = db.query(Conversation).filter(Conversation.id == conversation_id).first()
        if conv is None:
            raise HTTPException(status_code=404, detail="Not found")
        owns = access_service.is_admin(user) or conv.user_id == user.id
        if not owns or not check_scope_access(db, user, conv.scope_type, conv.scope_id):
            raise HTTPException(status_code=404, detail="Not found")
        return conv

    _dep._is_access_guard = True  # type: ignore[attr-defined]
    return _dep


def check_scope_access(db: Session, user: User, scope_type: str, scope_id: str) -> bool:
    """Used when creating/listing conversations by scope (no conversation_id
    yet to gate on): does the user have view access to the referenced
    document's case, or the case itself?"""
    if scope_type == "document":
        try:
            doc_id = int(scope_id)
        except (TypeError, ValueError):
            return False
        doc = db.query(Document).filter(Document.id == doc_id).first()
        if doc is None:
            return False
        return check_owned_or_case_access(
            db, user, owner_id=doc.owner_id, case_id=doc.case_id, edit=False
        )
    if scope_type == "case":
        return _check_case_id(db, user, scope_id, edit=False)
    return False
