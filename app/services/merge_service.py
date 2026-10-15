"""Merge a duplicate proceeding into another of its case, or a duplicate case into another.

Deterministic and idempotent-safe: every foreign key that points at the source
is repointed at the target, empty target fields are filled from the source, and
the source row is deleted. Nothing is merged across owners without edit access
(the API checks it); the services here only guard structure.
"""

import logging
import os
import shutil
from pathlib import Path

from sqlalchemy import func, update
from sqlalchemy.orm import Session

from app.core.validators import validate_case_id
from app.models.database import (
    ActionItem,
    ApiTokenCase,
    Case,
    CaseShare,
    Conversation,
    CostSignal,
    Document,
    Entity,
    IngestBatch,
    LegalCost,
    OAuthCode,
    Proceeding,
    UserSettings,
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
    db.delete(db.get(Proceeding, source_id))  # re-read after expire, never stale
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
            # Its row-move and audit row are flushed here; the commit below lands them.
            proc.case_id = target_id
            db.flush()
            merge_proceedings(db, proc.id, twin, actor_user_id, commit=False)
        else:
            proc.case_id = target_id
            db.flush()

    # The target's deadline rows win a (due date, type) clash. Two deadlines with
    # the same day and type collapse to one on purpose; do not widen the key.
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

    # Agent tokens scoped to the source case follow the merge.
    already_scoped = [
        t
        for (t,) in db.query(ApiTokenCase.token_id).filter(
            ApiTokenCase.case_id == target_id
        )
    ]
    db.query(ApiTokenCase).filter(
        ApiTokenCase.case_id == source_id, ApiTokenCase.token_id.in_(already_scoped)
    ).delete(synchronize_session=False)
    db.query(ApiTokenCase).filter(ApiTokenCase.case_id == source_id).update(
        {ApiTokenCase.case_id: target_id}, synchronize_session=False
    )

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
    from app.services.case_service import recompute_total_cost_exposure

    # Same transaction as the merge: one commit lands rows, delete, audit and total.
    recompute_total_cost_exposure(target_id, db, commit=False)
    db.commit()
    return db.get(Case, target_id)  # type: ignore[return-value]


def _rekey(mapping: object, old: str, new: str) -> dict | None:
    """``mapping`` with key ``old`` moved to ``new``; None when nothing changes."""
    if not isinstance(mapping, dict) or old not in mapping:
        return None
    out = dict(mapping)
    out[new] = out.pop(old)
    return out


def _move_case_dir(db: Session, old_id: str, new_id: str) -> tuple[Path, Path] | None:
    """Move ``DATA_DIR/<old>`` to ``<new>`` and repoint the stored relative paths."""
    from app.config import DATA_DIR  # late: tests repoint it

    root = os.path.realpath(DATA_DIR)
    old_dir = os.path.realpath(os.path.join(root, old_id))
    new_dir = os.path.realpath(os.path.join(root, new_id))
    if not (old_dir.startswith(root + os.sep) and new_dir.startswith(root + os.sep)):
        raise MergeError("invalid_case_id", "Unsafe case ID.")
    if not os.path.isdir(old_dir):
        return None
    if os.path.exists(new_dir):
        raise MergeError("case_id_taken", f"{new_id} already has stored files.")
    shutil.move(old_dir, new_dir)
    for column in (Document.file_path, IngestBatch.raw_source_path):
        db.query(column.class_).filter(column.startswith(f"{old_id}/")).update(
            {column: func.concat(new_id, func.substr(column, len(old_id) + 1))},
            synchronize_session=False,
        )
    return Path(old_dir), Path(new_dir)


def rename_case(
    db: Session, case_id: str, new_id: str, actor_user_id: int | None = None
) -> Case:
    """Change a case's internal ID; every FK follows through ON UPDATE CASCADE."""
    if case_id == "_TRIAGE" or new_id.strip().upper() == "_TRIAGE":
        raise MergeError("triage_case", "The Triage Inbox cannot be renamed.")
    validated = validate_case_id(new_id)
    if validated is None:
        raise MergeError(
            "invalid_case_id", "Use a case number such as 8245-25 or ADV-024-A."
        )
    new_id = validated
    if new_id == case_id:
        raise MergeError("same_target", "That is already the case ID.")
    if db.get(Case, case_id) is None:
        raise MergeError("not_found", "Case not found.")
    if db.get(Case, new_id) is not None:
        raise MergeError(
            "case_id_taken", f"{new_id} already exists. Merge into it instead."
        )

    db.execute(
        update(Case)
        .where(Case.id == case_id)
        .values(id=new_id)
        .execution_options(synchronize_session=False)
    )
    # References that are plain strings, not foreign keys.
    db.query(Conversation).filter(
        Conversation.scope_type == "case", Conversation.scope_id == case_id
    ).update({Conversation.scope_id: new_id}, synchronize_session=False)
    for row in db.query(UserSettings).all():
        data = dict(row.settings_json or {})
        changed = False
        for key in ("last_viewed_cases", "active_proceeding"):
            moved = _rekey(data.get(key), case_id, new_id)
            if moved is not None:
                data[key] = moved
                changed = True
        if changed:
            row.settings_json = data
    for code in db.query(OAuthCode).all():
        if case_id in (code.case_ids or []):
            code.case_ids = [new_id if c == case_id else c for c in code.case_ids]
    moved_dir = _move_case_dir(db, case_id, new_id)
    try:
        audit_service.record(
            db,
            AuditEventType.CASE_RENAMED,
            target_type="case",
            target_id=new_id,
            payload={"old_id": case_id, "new_id": new_id},
            actor_user_id=actor_user_id,
        )
        db.commit()
    except Exception:
        db.rollback()
        if moved_dir is not None:
            shutil.move(str(moved_dir[1]), str(moved_dir[0]))
        raise
    db.expire_all()
    return db.get(Case, new_id)  # type: ignore[return-value]
