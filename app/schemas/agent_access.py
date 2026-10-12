"""Settings → Agent access: tokens external AI agents use against the MCP endpoint."""

from __future__ import annotations

from datetime import datetime
from typing import Any, Literal

from pydantic import BaseModel, Field, field_validator

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
    # False when an admin switched MCP off for the whole instance.
    mcp_enabled: bool
    tokens: list[ApiTokenView]


class ApiTokenCreate(BaseModel):
    name: str = Field(min_length=1, max_length=80)
    access: ApiTokenAccess
    all_cases: bool
    case_ids: list[str] = []
    expires_in_days: int | None = Field(default=None, ge=1, le=3650)

    @field_validator("name")
    @classmethod
    def _name_is_readable(cls, value: str) -> str:
        value = " ".join(value.split())
        if not any(ch.isalnum() for ch in value):
            raise ValueError("Give the token a name with a letter or digit.")
        return value


class ApiTokenCreated(BaseModel):
    token: ApiTokenView
    # Shown once: only its hash is stored.
    secret: str


# --- Admin overview ------------------------------------------------------------

TokenStatus = Literal["active", "revoked", "expired"]


class AdminTokenView(ApiTokenView):
    owner_id: int
    owner_email: str
    owner_name: str | None
    status: TokenStatus
    # Audited MCP actions (writes) by this token in the last 7 days.
    activity_count_7d: int


class AdminAgentAccessView(BaseModel):
    mcp_enabled: bool
    tokens: list[AdminTokenView]


class McpSwitch(BaseModel):
    enabled: bool


class TokenActivityEntry(BaseModel):
    id: int
    created_at: datetime
    event_type: str
    tool: str | None
    target_type: str | None
    target_id: str | None
    detail: dict[str, Any]


class TokenActivityView(BaseModel):
    token_id: int
    entries: list[TokenActivityEntry]
