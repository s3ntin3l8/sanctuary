"""Admin → Agent access: every user's MCP API tokens and the instance-wide switch."""

from __future__ import annotations

from datetime import timedelta

from fastapi import APIRouter, Depends, Query
from sqlalchemy import func
from sqlalchemy.orm import Session, joinedload

from app.api.v1.errors import ApiError
from app.api.v1.settings_tokens import token_view
from app.core.timezone import now_utc
from app.dependencies import get_current_admin, get_db
from app.models.database import ApiToken, AuditLog, User
from app.schemas.agent_access import (
    AdminAgentAccessView,
    AdminTokenView,
    McpSwitch,
    TokenActivityEntry,
    TokenActivityView,
    TokenStatus,
)
from app.services import api_token_service

router = APIRouter(prefix="/admin/agent-access", tags=["admin"])

_ACTIVITY_WINDOW = timedelta(days=7)
# Payload keys already shown as columns; everything else is the entry's detail.
_ENVELOPE_KEYS = {"tool", "api_token_id"}


def _mcp_actions():
    """Audit rows written by an agent (actor_label ``mcp:<token name>``)."""
    return AuditLog.actor_label.like("mcp:%")


def _token_id_of_row():
    return AuditLog.payload["api_token_id"].astext


def _status(token: ApiToken) -> TokenStatus:
    if token.revoked_at is not None:
        return "revoked"
    if token.expires_at is not None and token.expires_at <= now_utc():
        return "expired"
    return "active"


def _view(db: Session) -> AdminAgentAccessView:
    tokens = (
        db.query(ApiToken)
        .options(joinedload(ApiToken.cases), joinedload(ApiToken.user))
        .order_by(ApiToken.created_at.desc())
        .all()
    )
    token_key = _token_id_of_row().label("token_key")
    activity = (
        db.query(token_key, func.count(AuditLog.id))
        .filter(_mcp_actions(), AuditLog.created_at >= now_utc() - _ACTIVITY_WINDOW)
        .group_by(token_key)
        .all()
    )
    counts: dict[str, int] = {row[0]: row[1] for row in activity}
    rows = []
    for t in tokens:
        owner: User = t.user
        rows.append(
            AdminTokenView(
                **token_view(db, t).model_dump(),
                owner_id=owner.id,
                owner_email=owner.email,
                owner_name=owner.display_name,
                status=_status(t),
                activity_count_7d=counts.get(str(t.id), 0),
            )
        )
    return AdminAgentAccessView(
        mcp_enabled=api_token_service.mcp_enabled(db), tokens=rows
    )


@router.get("", response_model=AdminAgentAccessView)
def admin_agent_access(
    db: Session = Depends(get_db), admin: User = Depends(get_current_admin)
):
    return _view(db)


@router.put("/enabled", response_model=AdminAgentAccessView)
def admin_set_mcp_enabled(
    body: McpSwitch,
    db: Session = Depends(get_db),
    admin: User = Depends(get_current_admin),
):
    api_token_service.set_mcp_enabled(db, body.enabled, actor_user_id=admin.id)
    db.commit()
    return _view(db)


def _token(db: Session, token_id: int) -> ApiToken:
    token = db.get(ApiToken, token_id)
    if token is None:
        raise ApiError(404, "token_not_found", "Token not found.")
    return token


@router.delete("/tokens/{token_id}", response_model=AdminAgentAccessView)
def admin_revoke_token(
    token_id: int,
    db: Session = Depends(get_db),
    admin: User = Depends(get_current_admin),
):
    api_token_service.revoke(
        db, _token(db, token_id), actor_user_id=admin.id, by_admin=True
    )
    db.commit()
    return _view(db)


@router.get("/tokens/{token_id}/activity", response_model=TokenActivityView)
def admin_token_activity(
    token_id: int,
    limit: int = Query(50, ge=1, le=200),
    db: Session = Depends(get_db),
    admin: User = Depends(get_current_admin),
):
    _token(db, token_id)
    rows = (
        db.query(AuditLog)
        .filter(_mcp_actions(), _token_id_of_row() == str(token_id))
        .order_by(AuditLog.created_at.desc(), AuditLog.id.desc())
        .limit(limit)
        .all()
    )
    return TokenActivityView(
        token_id=token_id,
        entries=[
            TokenActivityEntry(
                id=r.id,
                created_at=r.created_at,
                event_type=r.event_type.value,
                tool=(r.payload or {}).get("tool"),
                target_type=r.target_type,
                target_id=r.target_id,
                detail={
                    k: v
                    for k, v in (r.payload or {}).items()
                    if k not in _ENVELOPE_KEYS
                },
            )
            for r in rows
        ],
    )
