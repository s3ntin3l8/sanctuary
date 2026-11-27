"""The signed-in user as the SPA shell needs it."""

from __future__ import annotations

from typing import Literal

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
    timezone: str
    theme: Literal["system", "dark", "light"]
    # An AI endpoint in use is on the public internet, so document text leaves
    # this machine (ai_config.external_ai_in_use); the privacy badge says so.
    ai_external: bool
