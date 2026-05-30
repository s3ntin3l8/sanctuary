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

from app.models.database import Case, CaseShare, User
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
