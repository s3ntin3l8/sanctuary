"""Consent page for MCP clients using OAuth (Settings → Agent access, by another door)."""

from __future__ import annotations

from typing import Literal

from pydantic import BaseModel

from app.models.enums import ApiTokenAccess


class AuthorizationRequestView(BaseModel):
    client_name: str
    # Where the approval is sent: the redirect URI's host (or app scheme), shown
    # so the user can tell claude.ai from a look-alike.
    redirect_host: str
    mcp_enabled: bool


class AuthorizeDecision(BaseModel):
    client_id: str
    redirect_uri: str
    redirect_uri_provided_explicitly: bool = True
    code_challenge: str
    state: str | None = None
    resource: str | None = None
    decision: Literal["approve", "deny"]
    access: ApiTokenAccess = ApiTokenAccess.READ
    all_cases: bool = False
    case_ids: list[str] = []


class AuthorizeResult(BaseModel):
    # Where to send the browser next: the client's redirect URI with the code (or error).
    redirect_url: str
