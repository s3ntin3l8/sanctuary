"""Settings → Agent access: tokens external AI agents use against the MCP endpoint."""

from __future__ import annotations

from datetime import datetime

from pydantic import BaseModel, Field

from app.models.enums import ApiTokenAccess


class TokenCase(BaseModel):
    id: str
    title: str


class ApiTokenView(BaseModel):
    id: int
    name: str
    prefix: str
    access: ApiTokenAccess
    all_cases: bool
    cases: list[TokenCase]
    created_at: datetime
    last_used_at: datetime | None
    expires_at: datetime | None
    revoked_at: datetime | None


class AgentAccessView(BaseModel):
    mcp_url: str
    tokens: list[ApiTokenView]


class ApiTokenCreate(BaseModel):
    name: str = Field(min_length=1, max_length=80)
    access: ApiTokenAccess
    all_cases: bool
    case_ids: list[str] = []
    expires_in_days: int | None = Field(default=None, ge=1, le=3650)


class ApiTokenCreated(BaseModel):
    token: ApiTokenView
    # Shown once: only its hash is stored.
    secret: str
