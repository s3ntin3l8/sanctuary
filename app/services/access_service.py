"""Per-user case visibility and edit permission.

The single source of truth for "which cases can this user see / edit". Applied
at the route/repository layer — never as a model default — so background
workers (which operate without a user) always see every row.

- A user sees cases they **own** or that are **shared** with them (CaseShare).
- Admins see/edit everything.
- Edit requires owner, admin, or an EDITOR share; a VIEWER share is read-only.
"""

from __future__ import annotations

from sqlalchemy.orm import Session

from app.models.database import Case, CaseShare, ClaimEvidence, Document, User
from app.models.enums import CaseAccessLevel, UserRole


def is_admin(user: User | None) -> bool:
    return user is not None and user.role == UserRole.ADMIN


def _shared_case_ids(db: Session, user_id: int) -> set[str]:
    rows = db.query(CaseShare.case_id).filter(CaseShare.user_id == user_id).all()
    return {row[0] for row in rows}


def visible_case_ids(db: Session, user: User | None) -> set[str] | None:
    """Case ids the user may see.

    Returns ``None`` as a sentinel meaning *unrestricted* (admin) — callers
    skip filtering. Otherwise an explicit set (owned ∪ shared).
    """
    if is_admin(user):
        return None
    if user is None:
        return set()
    owned = {row[0] for row in db.query(Case.id).filter(Case.owner_id == user.id).all()}
    return owned | _shared_case_ids(db, user.id)


def can_see_object(
    user: User | None,
    visible: set[str] | None,
    *,
    owner_id: int | None,
    case_id: str | None,
) -> bool:
    """Per-row visibility test, given ``visible`` from `visible_case_ids`.

    Untriaged rows (no case, or the shared ``_TRIAGE`` bucket) are visible only
    to their owner (or an admin); case rows follow the case's visibility.
    """
    if not case_id or case_id == "_TRIAGE":
        return is_admin(user) or (
            user is not None and owner_id is not None and owner_id == user.id
        )
    return visible is None or case_id in visible


def editable_case_ids(db: Session, user: User | None) -> set[str] | None:
    """Case ids the user may edit (owner ∪ EDITOR shares).

    Returns ``None`` as a sentinel meaning *unrestricted* (admin), same
    convention as `visible_case_ids`.
    """
    if is_admin(user):
        return None
    if user is None:
        return set()
    owned = {row[0] for row in db.query(Case.id).filter(Case.owner_id == user.id).all()}
    editor_shares = {
        row[0]
        for row in db.query(CaseShare.case_id)
        .filter(
            CaseShare.user_id == user.id,
            CaseShare.permission == CaseAccessLevel.EDITOR,
        )
        .all()
    }
    return owned | editor_shares


def _share_permission(
    db: Session, user_id: int, case_id: str
) -> CaseAccessLevel | None:
    share = (
        db.query(CaseShare)
        .filter(CaseShare.user_id == user_id, CaseShare.case_id == case_id)
        .first()
    )
    return share.permission if share else None


def can_view_case(db: Session, user: User | None, case: Case | None) -> bool:
    """True when the user may view a specific case (owner, admin, or shared)."""
    if case is None:
        return False
    if is_admin(user):
        return True
    if user is None:
        return False
    if case.owner_id == user.id:
        return True
    return _share_permission(db, user.id, case.id) is not None


def can_edit_case(db: Session, user: User | None, case: Case | None) -> bool:
    """True when the user may mutate a case (owner, admin, or EDITOR share)."""
    if case is None:
        return False
    if is_admin(user):
        return True
    if user is None:
        return False
    if case.owner_id == user.id:
        return True
    return _share_permission(db, user.id, case.id) == CaseAccessLevel.EDITOR


def claim_linked_case_ids(db: Session, claim_id: int) -> set[str]:
    """Every distinct case_id reachable from a claim's evidence documents.

    Claims are global (see Claim's own docstring): the same claim can be
    evidence-linked from documents in different cases, potentially owned by
    different users. Callers must never expose the *identity or content* of
    linked cases the user cannot see (#159) — see `can_access_case_ids`.
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


def can_access_case_ids(
    db: Session, user: User, case_ids: set[str], *, edit: bool
) -> bool:
    """View = access to at least one case_id. Edit = edit access to every linked
    case the user can *see*; cases they cannot see are ignored entirely.

    Ignoring invisible cases is deliberate (#159): requiring edit access to
    them would make "edit denied" reveal that a claim is also referenced from
    a case the caller can't see. The accepted trade-off is that an edit to a
    claim shared across cases changes it for every linked case.
    """
    if not case_ids:
        return False
    cases = db.query(Case).filter(Case.id.in_(case_ids)).all()  # one query, not K
    visible = [c for c in cases if can_view_case(db, user, c)]
    if not visible:
        return False
    if not edit:
        return True
    return all(can_edit_case(db, user, c) for c in visible)


def claim_access_allowed(db: Session, user: User, claim_id: int, *, edit: bool) -> bool:
    case_ids = claim_linked_case_ids(db, claim_id)
    if case_ids:
        return can_access_case_ids(db, user, case_ids, edit=edit)
    owner_ids = claim_linked_owner_ids(db, claim_id)
    if not owner_ids:
        return False
    if is_admin(user):
        return True
    # Another user's untriaged documents are invisible to this user, so they
    # don't count (same rule as can_access_case_ids): owning one evidencing
    # document is enough to view or edit.
    return user.id in owner_ids
