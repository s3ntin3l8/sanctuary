"""Agent API tokens: mint, verify, revoke.

A token is a bearer secret (``sct_…``) that lets an external agent use the MCP
endpoint *as its owner*. Only the sha256 of the secret is stored, so a leaked
database cannot be replayed. Scope (which cases, read or read-write) lives on
the row; this module owns lifecycle, ``app/mcp`` applies the scope.
"""

from __future__ import annotations

import hashlib
import secrets
from datetime import datetime, timedelta

from sqlalchemy.orm import Session, joinedload

from app.core.timezone import now_utc
from app.models.database import ApiToken, ApiTokenCase, User
from app.models.enums import ApiTokenAccess, AuditEventType
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


def mint(
    db: Session,
    user: User,
    *,
    name: str,
    access: ApiTokenAccess,
    all_cases: bool,
    case_ids: list[str] | None = None,
    expires_at: datetime | None = None,
) -> tuple[ApiToken, str]:
    """Create a token for ``user`` and return it with its one-time secret."""
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

    secret = SECRET_PREFIX + secrets.token_urlsafe(32)
    token = ApiToken(
        user_id=user.id,
        name=name.strip(),
        token_hash=hash_secret(secret),
        prefix=secret[:_DISPLAY_PREFIX_LEN],
        access=access,
        all_cases=all_cases,
        expires_at=expires_at,
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
            "name": token.name,
            "access": access.value,
            "all_cases": all_cases,
            "case_ids": sorted(wanted),
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
        .options(joinedload(ApiToken.user))
        .filter(ApiToken.token_hash == hash_secret(secret))
        .first()
    )
    if token is None or token.revoked_at is not None or not token.user.is_active:
        return None
    if token.expires_at is not None and token.expires_at <= now_utc():
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


def revoke(db: Session, token: ApiToken, *, actor_user_id: int) -> None:
    if token.revoked_at is not None:
        return
    token.revoked_at = now_utc()
    audit_service.record(
        db,
        AuditEventType.API_TOKEN_REVOKED,
        target_type="api_token",
        target_id=str(token.id),
        payload={"name": token.name},
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
        .options(joinedload(ApiToken.cases))
        .filter(ApiToken.user_id == user.id)
        .order_by(ApiToken.created_at.desc())
        .all()
    )
