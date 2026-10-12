"""Action-item (deadline / court date) status changes."""

from __future__ import annotations

from sqlalchemy.orm import Session

from app.models.database import ActionItem
from app.models.enums import ActionItemStatus


def set_status(db: Session, item: ActionItem, status: ActionItemStatus) -> ActionItem:
    """Move ``item`` to ``status`` and commit. Access checks are the caller's job."""
    item.status = status
    db.commit()
    db.refresh(item)
    return item
