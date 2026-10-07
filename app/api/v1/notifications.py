"""The rail's 🔔 panel."""

from __future__ import annotations

from fastapi import APIRouter, Depends
from sqlalchemy.orm import Session

from app.dependencies import get_current_user, get_db
from app.models.database import User
from app.schemas.notifications import NotificationsView
from app.services.notifications_service import build_notifications

router = APIRouter(tags=["notifications"])


@router.get("/notifications", response_model=NotificationsView)
def notifications(
    db: Session = Depends(get_db), user: User = Depends(get_current_user)
):
    """Overdue and upcoming deadlines, hearings, the caller's pending triage
    bundles and overdue costs, each group counted in full and capped at five
    rows."""
    return build_notifications(db, user)
