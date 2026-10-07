"""Queries over the Gmail metadata index behind the history import page.

The index (``GmailMessageIndex``) mirrors the allowlisted mailbox's headers.
Everything here is read-side except ``upsert_metadata``/``assign_group_keys``
(called by the indexing task). "Ingested" is never stored: it is derived from
``ingest_batches`` at read time, so deleting a bundle makes its message
importable again with no bookkeeping.
"""

from __future__ import annotations

import base64
import json
from datetime import datetime
from typing import Any

from sqlalchemy import case, exists, func, or_
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.orm import Session

from app.core.timezone import now_utc
from app.models.database import Case, GmailMessageIndex, IngestBatch, Proceeding, User
from app.services import access_service
from app.services.ingestion.extractors import (
    extract_az_court_from_subject,
    extract_internal_id_from_subject,
)

UNREFERENCED = "unreferenced"
# One import request never queues more than this many messages.
MAX_IMPORT_MESSAGES = 500

_Idx = GmailMessageIndex


def _is_ingested():
    return exists().where(
        IngestBatch.owner_id == _Idx.owner_id,
        IngestBatch.message_id == _Idx.message_id,
    )


def upsert_metadata(db: Session, owner_id: int, metas: list[dict]) -> int:
    """Insert parsed Gmail metadata (``gmail.parse_metadata``); returns rows added."""
    if not metas:
        return 0
    now = now_utc()
    rows = []
    for meta in metas:
        subject = meta.get("subject") or ""
        rows.append(
            {
                **meta,
                "owner_id": owner_id,
                "internal_id": extract_internal_id_from_subject(subject),
                "az_court": extract_az_court_from_subject(subject),
                "indexed_at": now,
            }
        )
    result = db.execute(
        pg_insert(_Idx)
        .values(rows)
        .on_conflict_do_nothing(constraint="uq_gmail_index_owner_gmail")
    )
    return result.rowcount or 0  # type: ignore[attr-defined]


def indexed_gmail_ids(db: Session, owner_id: int) -> set[str]:
    return {row[0] for row in db.query(_Idx.gmail_id).filter(_Idx.owner_id == owner_id)}


def _own_ref(row: GmailMessageIndex) -> tuple[str, str] | None:
    if row.internal_id:
        return row.internal_id, "internal_id"
    if row.az_court:
        return row.az_court, "az_court"
    return None


def assign_group_keys(db: Session, owner_id: int) -> None:
    """Set each message's effective group: its own case reference, else the one
    carried by the oldest referenced message in its thread.

    A reply like "AW: Ihr Schreiben" has no reference of its own but belongs
    with the letter it answers.
    """
    rows = (
        db.query(_Idx)
        .filter(_Idx.owner_id == owner_id)
        .order_by(_Idx.sent_at, _Idx.id)
        .all()
    )
    thread_ref: dict[str, tuple[str, str]] = {}
    for row in rows:
        ref = _own_ref(row)
        if ref and row.thread_id not in thread_ref:
            thread_ref[row.thread_id] = ref
    for row in rows:
        ref = _own_ref(row) or thread_ref.get(row.thread_id)
        key, kind = ref if ref else (None, None)
        if (row.group_key, row.group_kind) != (key, kind):
            row.group_key, row.group_kind = key, kind
    db.flush()


def clear_index(db: Session, owner_id: int) -> None:
    """Forget the mirrored mailbox (on disconnect: a different account may follow)."""
    db.query(_Idx).filter(_Idx.owner_id == owner_id).delete(synchronize_session=False)
    db.flush()


def index_summary(db: Session, owner_id: int) -> tuple[int, datetime | None]:
    count, last = (
        db.query(func.count(_Idx.id), func.max(_Idx.indexed_at))
        .filter(_Idx.owner_id == owner_id)
        .one()
    )
    return count, last


def _matched_cases(db: Session, user: User, groups: list[tuple[str, str]]) -> dict:
    """(key, kind) -> Case.id for groups that already have a case.

    Mirrors ``_try_assign_case_from_subject``: only cases the user can edit, so
    the page never promises an auto-filing the importer would refuse.
    """
    editable = access_service.editable_case_ids(db, user)
    matched: dict[tuple[str, str], str] = {}

    internal = [key for key, kind in groups if kind == "internal_id"]
    if internal:
        for (case_id,) in db.query(Case.id).filter(Case.id.in_(internal)):
            if editable is None or case_id in editable:
                matched[(case_id, "internal_id")] = case_id

    az = [key for key, kind in groups if kind == "az_court"]
    if az:
        query = db.query(Proceeding.az_court, Proceeding.case_id).filter(
            Proceeding.az_court.in_(az)
        )
        if editable is not None:
            query = query.filter(Proceeding.case_id.in_(editable))
        for az_court, case_id in query:
            matched.setdefault((az_court, "az_court"), case_id)
    return matched


def list_groups(db: Session, user: User) -> list[dict[str, Any]]:
    """One row per case reference, oldest history first; unreferenced last."""
    rows = (
        db.query(
            _Idx.group_key,
            _Idx.group_kind,
            func.count(_Idx.id),
            func.sum(case((_is_ingested(), 1), else_=0)),
            func.min(_Idx.sent_at),
            func.max(_Idx.sent_at),
        )
        .filter(_Idx.owner_id == user.id)
        .group_by(_Idx.group_key, _Idx.group_kind)
        .all()
    )
    matched = _matched_cases(db, user, [(k, kind) for k, kind, *_ in rows if k])
    groups = [
        {
            "key": key or UNREFERENCED,
            "kind": kind,
            "matched_case_id": matched.get((key, kind)) if key else None,
            "count": count,
            "ingested_count": int(ingested or 0),
            "first_at": first,
            "last_at": last,
        }
        for key, kind, count, ingested, first, last in rows
    ]
    groups.sort(key=lambda g: (g["key"] == UNREFERENCED, g["first_at"]))
    return groups


def _encode_cursor(sent_at: datetime, row_id: int) -> str:
    raw = json.dumps([sent_at.isoformat(), row_id]).encode()
    return base64.urlsafe_b64encode(raw).decode()


def _decode_cursor(cursor: str) -> tuple[datetime, int]:
    try:
        sent_at, row_id = json.loads(base64.urlsafe_b64decode(cursor.encode()))
        return datetime.fromisoformat(sent_at), int(row_id)
    except Exception as exc:  # any malformed cursor is the caller's error
        raise ValueError("invalid cursor") from exc


def _group_filter(group: str):
    if group == UNREFERENCED:
        return _Idx.group_key.is_(None)
    return _Idx.group_key == group


def list_messages(
    db: Session,
    owner_id: int,
    *,
    group: str | None,
    cursor: str | None,
    limit: int,
) -> tuple[list[tuple[GmailMessageIndex, bool]], str | None]:
    """Messages oldest first (keyset-paginated), each with its ingested flag."""
    query = db.query(_Idx, _is_ingested()).filter(_Idx.owner_id == owner_id)
    if group:
        query = query.filter(_group_filter(group))
    if cursor:
        after_at, after_id = _decode_cursor(cursor)
        query = query.filter(
            or_(
                _Idx.sent_at > after_at,
                (_Idx.sent_at == after_at) & (_Idx.id > after_id),
            )
        )
    rows = query.order_by(_Idx.sent_at, _Idx.id).limit(limit + 1).all()
    next_cursor = None
    if len(rows) > limit:
        rows = rows[:limit]
        last = rows[-1][0]
        next_cursor = _encode_cursor(last.sent_at, last.id)
    return [(row, bool(ingested)) for row, ingested in rows], next_cursor


def select_for_import(
    db: Session,
    owner_id: int,
    *,
    gmail_ids: list[str] | None = None,
    group: str | None = None,
    oldest_n: int | None = None,
    before: datetime | None = None,
) -> list[str]:
    """Gmail ids to import: not yet ingested, oldest first, capped.

    ``gmail_ids`` (an explicit pick) wins over ``group``; ``oldest_n`` and
    ``before`` narrow either. Ids unknown to the index are ignored.
    """
    query = db.query(_Idx.gmail_id).filter(_Idx.owner_id == owner_id, ~_is_ingested())
    if gmail_ids:
        query = query.filter(_Idx.gmail_id.in_(gmail_ids))
    elif group:
        query = query.filter(_group_filter(group))
    if before:
        query = query.filter(_Idx.sent_at < before)
    limit = min(oldest_n or MAX_IMPORT_MESSAGES, MAX_IMPORT_MESSAGES)
    return [row[0] for row in query.order_by(_Idx.sent_at, _Idx.id).limit(limit)]
