"""Merge a duplicate proceeding into another of its case, or a duplicate case into another.

Deterministic and idempotent-safe: every foreign key that points at the source
is repointed at the target, empty target fields are filled from the source, and
the source row is deleted. Nothing is merged across owners without edit access
(the API checks it); the services here only guard structure.
"""

import logging

from sqlalchemy.orm import Session

from app.models.database import (
    ActionItem,
    Case,
    CaseShare,
    Conversation,
    CostSignal,
    Document,
    Entity,
    IngestBatch,
    LegalCost,
    Proceeding,
)
from app.models.enums import (
    AuditEventType,
    CaseAccessLevel,
    ProceedingStatus,
)
from app.services import audit_service

logger = logging.getLogger(__name__)

# Tables that carry proceeding_id / case_id (see the FK list on `proceedings` / `cases`).
_PROCEEDING_REFS = (Document, IngestBatch, ActionItem, LegalCost, CostSignal)
_CASE_REFS = (Document, IngestBatch, ActionItem, LegalCost, CostSignal, Entity)


class MergeError(ValueError):
    """The merge is structurally invalid; ``code`` is the API error code."""

    def __init__(self, code: str, message: str):
        super().__init__(message)
        self.code = code


def merge_proceedings(
    db: Session,
    source_id: int,
    target_id: int,
    actor_user_id: int | None = None,
    commit: bool = True,
) -> Proceeding:
    """Fold proceeding ``source_id`` into ``target_id`` (same case) and delete it."""
    if source_id == target_id:
        raise MergeError("same_target", "Choose a different proceeding to merge into.")
    source = db.get(Proceeding, source_id)
    target = db.get(Proceeding, target_id)
    if source is None or target is None:
        raise MergeError("not_found", "Proceeding not found.")
    if source.case_id != target.case_id:
        raise MergeError(
            "cross_case", "Proceedings can only be merged within the same case."
        )

    moved = 0
    for model in _PROCEEDING_REFS:
        moved += (
            db.query(model)
            .filter(model.proceeding_id == source.id)
            .update({model.proceeding_id: target.id}, synchronize_session=False)
        )

    if not target.subject_matter and source.subject_matter:
        target.subject_matter = source.subject_matter
    if not target.az_court and source.az_court:
        target.az_court = source.az_court
    starts = [d for d in (target.started_at, source.started_at) if d is not None]
    if starts:
        target.started_at = min(starts)
    if source.status == ProceedingStatus.ACTIVE:
        target.status = ProceedingStatus.ACTIVE
        target.ended_at = None
    if not source.is_draft:
        target.is_draft = False

    case_id = source.case_id
    db.flush()  # expire_all() below would discard the pending field edits
    db.expire_all()
    db.delete(db.get(Proceeding, source_id))
    audit_service.record(
        db,
        AuditEventType.PROCEEDING_MERGED,
        target_type="proceeding",
        target_id=str(target_id),
        payload={"source_id": source_id, "case_id": case_id, "rows_moved": moved},
        actor_user_id=actor_user_id,
    )
    if commit:
        db.commit()
    else:
        db.flush()
    return db.get(Proceeding, target_id)  # type: ignore[return-value]


def _merge_names(first: list | None, second: list | None, key=None) -> list | None:
    """Union two party lists, keeping order and dropping repeats by name."""
    out: list = []
    seen: set[str] = set()
    for item in [*(first or []), *(second or [])]:
        name = (key(item) if key else item) or ""
        norm = str(name).strip().lower()
        if norm and norm in seen:
            continue
        seen.add(norm)
        out.append(item)
    return out or None


def merge_cases(
    db: Session, source_id: str, target_id: str, actor_user_id: int | None = None
) -> Case:
    """Fold case ``source_id`` into ``target_id`` and delete it."""
    if source_id == target_id:
        raise MergeError("same_target", "Choose a different case to merge into.")
    if "_TRIAGE" in (source_id, target_id):
        raise MergeError("triage_case", "The Triage Inbox cannot be merged.")
    source = db.get(Case, source_id)
    target = db.get(Case, target_id)
    if source is None or target is None:
        raise MergeError("not_found", "Case not found.")

    # Proceedings first: one the target already has (same AZ) absorbs its twin.
    target_by_az = {
        p.az_court: p.id
        for p in db.query(Proceeding).filter(Proceeding.case_id == target_id)
        if p.az_court
    }
    for proc in db.query(Proceeding).filter(Proceeding.case_id == source_id).all():
        twin = target_by_az.get(proc.az_court) if proc.az_court else None
        if twin is not None:
            # merge_proceedings requires one case; repoint the proceeding first.
            proc.case_id = target_id
            db.flush()
            merge_proceedings(db, proc.id, twin, actor_user_id, commit=False)
        else:
            proc.case_id = target_id
            db.flush()

    # The target's deadline rows win a (due date, type) clash.
    taken = {
        (a.due_date, a.action_type)
        for a in db.query(ActionItem).filter(ActionItem.case_id == target_id)
    }
    for item in db.query(ActionItem).filter(ActionItem.case_id == source_id).all():
        if (item.due_date, item.action_type) in taken:
            db.delete(item)
        else:
            taken.add((item.due_date, item.action_type))
    db.flush()

    for model in _CASE_REFS:
        db.query(model).filter(model.case_id == source_id).update(
            {model.case_id: target_id}, synchronize_session=False
        )
    db.query(Conversation).filter(
        Conversation.scope_type == "case", Conversation.scope_id == source_id
    ).update({Conversation.scope_id: target_id}, synchronize_session=False)

    # Sharing: union, keeping the higher level; the source owner stays involved.
    shares = {
        s.user_id: s for s in db.query(CaseShare).filter(CaseShare.case_id == target_id)
    }
    incoming = [
        (s.user_id, s.permission)
        for s in db.query(CaseShare).filter(CaseShare.case_id == source_id)
    ]
    if source.owner_id is not None and source.owner_id != target.owner_id:
        incoming.append((source.owner_id, CaseAccessLevel.EDITOR))
    for user_id, level in incoming:
        if user_id == target.owner_id:
            continue
        existing = shares.get(user_id)
        if existing is None:
            share = CaseShare(case_id=target_id, user_id=user_id, permission=level)
            db.add(share)
            shares[user_id] = share
        elif level == CaseAccessLevel.EDITOR:
            existing.permission = CaseAccessLevel.EDITOR

    target.parties = _merge_names(
        target.parties, source.parties, key=lambda p: p.get("name")
    )
    target.opposing_parties = _merge_names(
        target.opposing_parties, source.opposing_parties
    )
    if target.is_draft and not source.is_draft:
        target.is_draft = False
    # The brief describes the old document set; let the next run rebuild it.
    target.brief_queued_at = None

    db.flush()  # expire_all() below would discard the pending field edits
    db.expire_all()
    # Row-level delete: the ORM cascade would act on stale relationship state.
    db.query(Case).filter(Case.id == source_id).delete(synchronize_session=False)
    audit_service.record(
        db,
        AuditEventType.CASE_MERGED,
        target_type="case",
        target_id=target_id,
        payload={"source_id": source_id},
        actor_user_id=actor_user_id,
    )
    db.commit()

    from app.services.case_service import recompute_total_cost_exposure

    recompute_total_cost_exposure(target_id, db)
    return db.get(Case, target_id)  # type: ignore[return-value]
