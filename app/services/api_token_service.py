"""Agent API tokens: mint, verify, revoke.

A token is a bearer secret (``sct_…``) that lets an external agent use the MCP
endpoint *as its owner*. Only the sha256 of the secret is stored, so a leaked
database cannot be replayed. Scope (which cases, read or read-write) lives on
the row; this module owns lifecycle, ``app/mcp`` applies the scope.
"""

from __future__ import annotations

import hashlib
import secrets
from collections.abc import Iterable
from datetime import datetime, timedelta

from sqlalchemy.orm import Session, joinedload

from app.core.cache import cache
from app.core.timezone import now_utc
from app.models.database import ApiToken, ApiTokenCase, AppSettings, User
from app.models.enums import ApiTokenAccess, ApiTokenKind, AuditEventType
from app.services import access_service, audit_service

SECRET_PREFIX = "sct_"  # pragma: allowlist secret
_DISPLAY_PREFIX_LEN = len(SECRET_PREFIX) + 4
# `last_used_at` is a hint for the user, not an audit trail; skip the write when
# it was refreshed recently so a chatty agent doesn't turn every call into one.
_LAST_USED_RESOLUTION = timedelta(minutes=1)


class TokenScopeError(ValueError):
    """The requested scope is empty or names a case the user cannot see."""


def hash_secret(secret: str) -> str:
    return hashlib.sha256(secret.encode()).hexdigest()


def _scope_case_ids(
    db: Session, user: User, *, all_cases: bool, case_ids: list[str] | None
) -> set[str]:
    """The case ids a token may be scoped to; raises if the scope is not allowed."""
    wanted = set(case_ids or [])
    if all_cases:
        if wanted:
            raise TokenScopeError("All-cases tokens take no case list.")
    elif not wanted:
        raise TokenScopeError("Pick at least one case, or choose all cases.")
    else:
        # None = admin: an admin may scope a token to any case.
        visible = access_service.visible_case_ids(db, user)
        if visible is not None:
            unknown = wanted - visible
            if unknown:
                raise TokenScopeError(f"Unknown case: {sorted(unknown)[0]}")
    return wanted


def check_scope(
    db: Session, user: User, *, all_cases: bool, case_ids: list[str] | None
) -> None:
    _scope_case_ids(db, user, all_cases=all_cases, case_ids=case_ids)


def mint(
    db: Session,
    user: User,
    *,
    name: str,
    access: ApiTokenAccess,
    all_cases: bool,
    case_ids: list[str] | None = None,
    expires_at: datetime | None = None,
    kind: ApiTokenKind = ApiTokenKind.STATIC,
    oauth_client_id: str | None = None,
) -> tuple[ApiToken, str]:
    """Create a token for ``user`` and return it with its one-time secret."""
    wanted = _scope_case_ids(db, user, all_cases=all_cases, case_ids=case_ids)

    secret = SECRET_PREFIX + secrets.token_urlsafe(32)
    token = ApiToken(
        user_id=user.id,
        name=name.strip(),
        token_hash=hash_secret(secret),
        prefix=secret[:_DISPLAY_PREFIX_LEN],
        access=access,
        all_cases=all_cases,
        expires_at=expires_at,
        kind=kind,
        oauth_client_id=oauth_client_id,
        cases=[ApiTokenCase(case_id=cid) for cid in sorted(wanted)],
    )
    db.add(token)
    db.flush()
    audit_service.record(
        db,
        AuditEventType.API_TOKEN_CREATED,
        target_type="api_token",
        target_id=str(token.id),
        payload={
            "api_token_id": token.id,
            "name": token.name,
            "access": access.value,
            "all_cases": all_cases,
            "case_ids": sorted(wanted),
            "kind": kind.value,
            "oauth_client_id": oauth_client_id,
        },
        actor_user_id=user.id,
    )
    return token, secret


def verify(db: Session, secret: str) -> ApiToken | None:
    """The live token for ``secret``, or None (unknown, revoked, expired, owner disabled).

    A pure read; the caller records the use with :func:`touch`.
    """
    if not secret.startswith(SECRET_PREFIX):
        return None
    token = (
        db.query(ApiToken)
        .options(joinedload(ApiToken.user), joinedload(ApiToken.grant))
        .filter(ApiToken.token_hash == hash_secret(secret))
        .first()
    )
    if token is None or token.revoked_at is not None or not token.user.is_active:
        return None
    now = now_utc()
    if token.expires_at is not None and token.expires_at <= now:
        return None
    # An OAuth token's secret is a short-lived access token (renewed by refresh).
    if token.kind == ApiTokenKind.OAUTH and (
        token.grant is None or token.grant.access_expires_at <= now
    ):
        return None
    return token


def touch(db: Session, token: ApiToken) -> None:
    """Stamp ``last_used_at`` (at most once a minute) and commit ``db``.

    The auth gate calls this on its own short-lived session, outside any
    request transaction; keep it to that single column.
    """
    now = now_utc()
    if token.last_used_at is None or now - token.last_used_at > _LAST_USED_RESOLUTION:
        token.last_used_at = now
        db.commit()


def revoke(
    db: Session,
    token: ApiToken,
    *,
    actor_user_id: int,
    by_admin: bool = False,
    reason: str | None = None,
) -> None:
    if token.revoked_at is not None:
        return
    token.revoked_at = now_utc()
    audit_service.record(
        db,
        AuditEventType.API_TOKEN_REVOKED,
        target_type="api_token",
        target_id=str(token.id),
        payload={
            "api_token_id": token.id,
            "name": token.name,
            "owner_user_id": token.user_id,
            "by_admin": by_admin,
            "reason": reason,
        },
        actor_user_id=actor_user_id,
    )


def get_for_user(db: Session, user: User, token_id: int) -> ApiToken | None:
    return (
        db.query(ApiToken)
        .filter(ApiToken.id == token_id, ApiToken.user_id == user.id)
        .first()
    )


def list_for_user(db: Session, user: User) -> list[ApiToken]:
    return (
        db.query(ApiToken)
        .options(
            joinedload(ApiToken.cases),
            joinedload(ApiToken.grant),
            joinedload(ApiToken.oauth_client),
        )
        .filter(ApiToken.user_id == user.id)
        .order_by(ApiToken.created_at.desc())
        .all()
    )


def token_names(db: Session, ids: Iterable[int | None]) -> dict[int | None, str]:
    """Names of the tokens behind agent-written rows, in one query.

    Rows tagged with a since-deleted token have a NULL id and simply don't
    appear; callers read ``names.get(row.agent_token_id)``.
    """
    wanted = {i for i in ids if i is not None}
    if not wanted:
        return {}
    rows = db.query(ApiToken.id, ApiToken.name).filter(ApiToken.id.in_(wanted)).all()
    names: dict[int | None, str] = {row[0]: row[1] for row in rows}
    return names


# --- Instance-wide switch ------------------------------------------------------

_MCP_ENABLED_KEY = "mcp_enabled"
_SWITCH_CACHE_KEY = "mcp:instance_enabled"
# seconds; the gate reads this on every /mcp request. The cache is per process:
# a toggle takes effect immediately in the worker that handled it and within this
# TTL in any other uvicorn worker.
_SWITCH_CACHE_TTL = 5


def mcp_enabled(db: Session) -> bool:
    """Whether the MCP endpoint is switched on for this instance (default: yes)."""
    cached = cache.get(_SWITCH_CACHE_KEY)
    if cached is not None:
        return bool(cached)
    row = db.query(AppSettings).first()
    settings = row.settings_json if row and isinstance(row.settings_json, dict) else {}
    value = bool(settings.get(_MCP_ENABLED_KEY, True))
    cache.set(_SWITCH_CACHE_KEY, value, ttl=_SWITCH_CACHE_TTL)
    return value


def set_mcp_enabled(db: Session, value: bool, *, actor_user_id: int) -> None:
    row = db.query(AppSettings).first()
    if row is None:
        row = AppSettings(settings_json={})
        db.add(row)
        db.flush()
    previous = bool((row.settings_json or {}).get(_MCP_ENABLED_KEY, True))
    row.settings_json = {**(row.settings_json or {}), _MCP_ENABLED_KEY: bool(value)}
    db.flush()
    cache.delete(_SWITCH_CACHE_KEY)
    audit_service.record(
        db,
        AuditEventType.MCP_SWITCH_CHANGED,
        target_type="instance",
        payload={"enabled": bool(value), "previous": previous},
        actor_user_id=actor_user_id,
    )
