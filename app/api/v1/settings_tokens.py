"""Settings → Agent access: mint and revoke the user's MCP API tokens."""

from __future__ import annotations

from datetime import timedelta

from fastapi import APIRouter, Depends, Request
from sqlalchemy.orm import Session

from app.api.v1.errors import ApiError
from app.core.timezone import now_utc
from app.dependencies import get_current_user, get_db
from app.models.database import ApiToken, Case, User
from app.schemas.agent_access import (
    AgentAccessView,
    ApiTokenCreate,
    ApiTokenCreated,
    ApiTokenView,
    TokenCase,
)
from app.services import api_token_service

router = APIRouter(prefix="/settings/agent-access", tags=["settings"])


def _view(db: Session, token: ApiToken) -> ApiTokenView:
    ids = [c.case_id for c in token.cases]
    titles: dict[str, str] = {}
    if ids:
        rows = db.query(Case.id, Case.title).filter(Case.id.in_(ids)).all()
        titles = {row[0]: row[1] for row in rows}
    return ApiTokenView(
        id=token.id,
        name=token.name,
        prefix=token.prefix,
        access=token.access,
        all_cases=token.all_cases,
        cases=[TokenCase(id=i, title=titles.get(i, i)) for i in sorted(ids)],
        created_at=token.created_at,
        last_used_at=token.last_used_at,
        expires_at=token.expires_at,
        revoked_at=token.revoked_at,
    )


@router.get("", response_model=AgentAccessView)
def agent_access(
    request: Request,
    db: Session = Depends(get_db),
    user: User = Depends(get_current_user),
):
    return AgentAccessView(
        mcp_url=f"{request.base_url}mcp",
        tokens=[_view(db, t) for t in api_token_service.list_for_user(db, user)],
    )


@router.post("", response_model=ApiTokenCreated, status_code=201)
def create_token(
    body: ApiTokenCreate,
    db: Session = Depends(get_db),
    user: User = Depends(get_current_user),
):
    expires_at = (
        now_utc() + timedelta(days=body.expires_in_days)
        if body.expires_in_days
        else None
    )
    try:
        token, secret = api_token_service.mint(
            db,
            user,
            name=body.name,
            access=body.access,
            all_cases=body.all_cases,
            case_ids=body.case_ids,
            expires_at=expires_at,
        )
    except api_token_service.TokenScopeError as exc:
        raise ApiError(422, "invalid_scope", str(exc)) from exc
    db.commit()
    return ApiTokenCreated(token=_view(db, token), secret=secret)


@router.delete("/{token_id}", response_model=ApiTokenView)
def revoke_token(
    token_id: int,
    db: Session = Depends(get_db),
    user: User = Depends(get_current_user),
):
    token = api_token_service.get_for_user(db, user, token_id)
    if token is None:
        raise ApiError(404, "token_not_found", "Token not found.")
    api_token_service.revoke(db, token, actor_user_id=user.id)
    db.commit()
    return _view(db, token)
