"""Action-item (deadline / court date) changes."""

from __future__ import annotations

from datetime import datetime

from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from app.models.database import ActionItem
from app.models.enums import ActionItemStatus


class ActionItemClash(Exception):
    """Another item of the same type already falls on that date."""


def set_status(db: Session, item: ActionItem, status: ActionItemStatus) -> ActionItem:
    """Move ``item`` to ``status`` and commit. Access checks are the caller's job."""
    item.status = status
    db.commit()
    db.refresh(item)
    return item


def update(
    db: Session,
    item: ActionItem,
    *,
    title: str | None = None,
    due_date: datetime | None = None,
) -> ActionItem:
    """Correct the title and/or due date and commit; a due date that collides
    with another item of the same type raises :class:`ActionItemClash`."""
    try:
        with db.begin_nested():
            if title is not None:
                item.title = title
            if due_date is not None:
                item.due_date = due_date
    except IntegrityError:
        raise ActionItemClash from None
    db.commit()
    db.refresh(item)
    return item
