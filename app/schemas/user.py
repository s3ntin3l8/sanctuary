"""The signed-in user as the SPA shell needs it."""

from __future__ import annotations

from pydantic import BaseModel

from app.models.enums import UserRole


class CurrentUser(BaseModel):
    id: int
    email: str
    display_name: str | None
    role: UserRole


class ShellView(BaseModel):
    """Everything the persistent app shell (rail, profile menu) renders."""

    user: CurrentUser
    triage_count: int
