"""OAuth 2.1 authorization server state for MCP clients.

Sanctuary is its own authorization server. A client registers (public, PKCE
S256 only), the user approves on the consent page which stores a one-time code
carrying the chosen scope, and redeeming the code mints an ordinary OAuth
``ApiToken`` plus an ``OAuthGrant`` holding the rotating access/refresh secrets.
Everything else (scope, read/write, tagging, audit, revocation) is the existing
API-token machinery. Secrets are random and stored only as sha256 hashes.
"""

from __future__ import annotations

import re
import secrets
from datetime import datetime, timedelta
from urllib.parse import urlparse

from mcp.shared.auth import OAuthClientInformationFull
from sqlalchemy import delete
from sqlalchemy.orm import Session

from app.core.timezone import now_utc
from app.models.database import (
    ApiToken,
    OAuthClient,
    OAuthCode,
    OAuthGrant,
    User,
)
from app.models.enums import ApiTokenAccess, ApiTokenKind
from app.services import api_token_service

CODE_TTL = timedelta(minutes=10)
ACCESS_TTL = timedelta(hours=1)
REFRESH_TTL = timedelta(days=30)  # sliding: renewed on every refresh
REFRESH_PREFIX = "scr_"  # pragma: allowlist secret
MAX_CLIENTS = 500

# S256 challenge = base64url(sha256(verifier)) without padding: always 43 chars.
_CHALLENGE_RE = re.compile(r"^[A-Za-z0-9_-]{43}$")
_LOOPBACK = {"localhost", "127.0.0.1", "[::1]", "::1"}
# Schemes that run code or read local files when navigated to.
_FORBIDDEN_SCHEMES = {"javascript", "data", "file", "vbscript", "about", "blob", "ftp"}


class OAuthError(Exception):
    """A client/user error with an RFC 6749-style code."""

    def __init__(self, error: str, description: str) -> None:
        super().__init__(description)
        self.error = error
        self.description = description


def _hash(secret: str) -> str:
    return api_token_service.hash_secret(secret)


def redirect_uri_allowed(uri: str) -> bool:
    """https, loopback http (RFC 8252), or a private-use app scheme; no fragments.

    Any scheme outside ``_FORBIDDEN_SCHEMES`` counts as a private-use app scheme
    (RFC 8252 §7.1) by design; tighten with an explicit allow-list, not by
    growing the forbidden set.
    """
    parsed = urlparse(uri)
    if parsed.fragment or not parsed.scheme:
        return False
    if parsed.scheme == "https":
        return bool(parsed.hostname)
    if parsed.scheme == "http":
        return parsed.hostname in _LOOPBACK or f"[{parsed.hostname}]" in _LOOPBACK
    return parsed.scheme not in _FORBIDDEN_SCHEMES


# --- Clients -------------------------------------------------------------------


def register_client(db: Session, info: OAuthClientInformationFull) -> None:
    if info.token_endpoint_auth_method != "none":
        raise OAuthError(
            "invalid_client_metadata",
            "Only public clients are supported: use token_endpoint_auth_method "
            "'none' with PKCE.",
        )
    uris = [str(u) for u in info.redirect_uris or []]
    if not uris or not all(redirect_uri_allowed(u) for u in uris):
        raise OAuthError(
            "invalid_redirect_uri",
            "redirect_uris must be https, loopback http, or an app-specific scheme.",
        )
    if db.query(OAuthClient).count() >= MAX_CLIENTS:
        raise OAuthError("invalid_client_metadata", "Too many registered clients.")
    db.add(OAuthClient(client_id=info.client_id, info=info.model_dump(mode="json")))


def get_client(db: Session, client_id: str) -> OAuthClientInformationFull | None:
    row = db.get(OAuthClient, client_id)
    return OAuthClientInformationFull.model_validate(row.info) if row else None


def client_display_name(info: OAuthClientInformationFull) -> str:
    return (info.client_name or "").strip() or "An MCP client"


# --- Consent -------------------------------------------------------------------


def check_authorization_request(
    db: Session, *, client_id: str, redirect_uri: str, code_challenge: str
) -> OAuthClientInformationFull:
    """Validate the parameters echoed back by the consent page."""
    client = get_client(db, client_id)
    if client is None:
        raise OAuthError("invalid_request", "Unknown client.")
    if redirect_uri not in {str(u) for u in client.redirect_uris or []}:
        raise OAuthError("invalid_request", "redirect_uri is not registered.")
    if not _CHALLENGE_RE.match(code_challenge):
        raise OAuthError(
            "invalid_request", "code_challenge must be a PKCE S256 challenge."
        )
    return client


def create_code(
    db: Session,
    user: User,
    client: OAuthClientInformationFull,
    *,
    redirect_uri: str,
    redirect_uri_provided_explicitly: bool,
    code_challenge: str,
    resource: str | None,
    access: ApiTokenAccess,
    all_cases: bool,
    case_ids: list[str],
) -> str:
    """Store the user's approval; returns the one-time authorization code."""
    try:
        api_token_service.check_scope(db, user, all_cases=all_cases, case_ids=case_ids)
    except api_token_service.TokenScopeError as exc:
        raise OAuthError("invalid_scope", str(exc)) from exc
    code = secrets.token_urlsafe(32)
    db.add(
        OAuthCode(
            code_hash=_hash(code),
            client_id=client.client_id,
            user_id=user.id,
            redirect_uri=redirect_uri,
            redirect_uri_provided_explicitly=redirect_uri_provided_explicitly,
            code_challenge=code_challenge,
            resource=resource,
            access=access,
            all_cases=all_cases,
            case_ids=[] if all_cases else sorted(set(case_ids)),
            expires_at=now_utc() + CODE_TTL,
        )
    )
    return code


# --- Token endpoint ------------------------------------------------------------


def load_code(db: Session, client_id: str, code: str) -> OAuthCode | None:
    row = db.get(OAuthCode, _hash(code))
    return row if row is not None and row.client_id == client_id else None


def _issue(token: ApiToken, grant: OAuthGrant, *, now: datetime) -> tuple[str, str]:
    """Rotate the grant's access and refresh secrets onto ``token``."""
    access = api_token_service.SECRET_PREFIX + secrets.token_urlsafe(32)
    refresh = REFRESH_PREFIX + secrets.token_urlsafe(32)
    token.token_hash = _hash(access)
    token.prefix = access[: len(api_token_service.SECRET_PREFIX) + 4]
    grant.previous_refresh_hash = grant.refresh_hash or None
    grant.refresh_hash = _hash(refresh)
    grant.access_expires_at = now + ACCESS_TTL
    grant.refresh_expires_at = now + REFRESH_TTL
    return access, refresh


def redeem_code(db: Session, code: OAuthCode) -> tuple[str, str, int]:
    """Exchange a code for (access token, refresh token, expires_in); single use."""
    # Delete first and check the row count: a concurrent second redemption of the
    # same code loses the race and is rejected.
    deleted = db.execute(delete(OAuthCode).where(OAuthCode.code_hash == code.code_hash))
    if deleted.rowcount != 1:  # type: ignore[attr-defined]
        raise OAuthError("invalid_grant", "Authorization code already used.")
    user = db.get(User, code.user_id)
    client = db.get(OAuthClient, code.client_id)
    if user is None or not user.is_active or client is None:
        raise OAuthError("invalid_grant", "The authorization is no longer valid.")
    name = client_display_name(OAuthClientInformationFull.model_validate(client.info))
    try:
        token, _ = api_token_service.mint(
            db,
            user,
            name=name,
            access=code.access,
            all_cases=code.all_cases,
            case_ids=list(code.case_ids),
            kind=ApiTokenKind.OAUTH,
            oauth_client_id=code.client_id,
        )
    except api_token_service.TokenScopeError as exc:
        raise OAuthError("invalid_grant", str(exc)) from exc
    now = now_utc()
    grant = OAuthGrant(
        api_token_id=token.id,
        refresh_hash="",
        refresh_expires_at=now,
        access_expires_at=now,
        resource=code.resource,
    )
    db.add(grant)
    access, refresh = _issue(token, grant, now=now)
    db.flush()
    return access, refresh, int(ACCESS_TTL.total_seconds())


def _live_grant(db: Session, refresh_hash: str, client_id: str) -> OAuthGrant | None:
    grant = (
        db.query(OAuthGrant)
        .filter(OAuthGrant.refresh_hash == refresh_hash)
        .with_for_update()
        .first()
    )
    if grant is None:
        return None
    token = db.get(ApiToken, grant.api_token_id)
    if (
        token is None
        or token.oauth_client_id != client_id
        or token.revoked_at is not None
        or not token.user.is_active
        or grant.refresh_expires_at <= now_utc()
    ):
        return None
    return grant


def load_refresh(db: Session, client_id: str, refresh: str) -> OAuthGrant | None:
    """The grant for a live refresh token. Presenting an already-rotated token
    means it leaked: the whole grant is revoked."""
    refresh_hash = _hash(refresh)
    grant = _live_grant(db, refresh_hash, client_id)
    if grant is not None:
        return grant
    stale = (
        db.query(OAuthGrant)
        .filter(OAuthGrant.previous_refresh_hash == refresh_hash)
        .first()
    )
    if stale is not None:
        token = db.get(ApiToken, stale.api_token_id)
        if token is not None and token.oauth_client_id == client_id:
            api_token_service.revoke(
                db,
                token,
                actor_user_id=token.user_id,
                reason="refresh_token_reuse",
            )
    return None


def refresh_grant(db: Session, grant: OAuthGrant) -> tuple[str, str, int]:
    token = db.get(ApiToken, grant.api_token_id)
    assert token is not None  # cascade guarantees it
    access, refresh = _issue(token, grant, now=now_utc())
    db.flush()
    return access, refresh, int(ACCESS_TTL.total_seconds())


# --- Revocation ----------------------------------------------------------------


def grant_for_secret(db: Session, secret: str) -> ApiToken | None:
    """The OAuth token behind an access or refresh secret (for RFC 7009 revoke)."""
    token = api_token_service.verify(db, secret)
    if token is not None and token.kind == ApiTokenKind.OAUTH:
        return token
    grant = (
        db.query(OAuthGrant).filter(OAuthGrant.refresh_hash == _hash(secret)).first()
    )
    return db.get(ApiToken, grant.api_token_id) if grant else None
