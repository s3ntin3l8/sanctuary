"""The persistent app shell: who is signed in and the rail badge counts."""

from __future__ import annotations

from fastapi import APIRouter, Depends
from sqlalchemy.orm import Session

from app.dependencies import get_current_user, get_db
from app.helpers import triage_inbox_count
from app.models.database import User
from app.schemas.user import CurrentUser, ShellView

router = APIRouter(tags=["shell"])


@router.get("/shell", response_model=ShellView)
def shell(db: Session = Depends(get_db), user: User = Depends(get_current_user)):
    return ShellView(
        user=CurrentUser(
            id=user.id, email=user.email, display_name=user.display_name, role=user.role
        ),
        triage_count=triage_inbox_count(db, user.id),
    )
