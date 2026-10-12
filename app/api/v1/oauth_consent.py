"""Consent step of the OAuth flow MCP clients use (see ``app.mcp.oauth``).

The SDK's ``/authorize`` endpoint validates the client's request and redirects
the browser to the SPA consent page; the page talks to these two routes with the
user's session. Approving stores a one-time code carrying the scope the user
chose; the client redeems it at ``/token`` for the actual credential.
"""

from __future__ import annotations

from urllib.parse import urlparse

from fastapi import APIRouter, Depends, Query
from mcp.server.auth.provider import construct_redirect_uri
from sqlalchemy.orm import Session

from app.api.v1.errors import ApiError
from app.dependencies import get_current_user, get_db
from app.models.database import User
from app.schemas.oauth import (
    AuthorizationRequestView,
    AuthorizeDecision,
    AuthorizeResult,
)
from app.services import api_token_service, oauth_service
from app.services.oauth_service import OAuthError

router = APIRouter(prefix="/oauth", tags=["oauth"])


def _redirect_host(uri: str) -> str:
    parsed = urlparse(uri)
    return (
        parsed.netloc if parsed.scheme in ("http", "https") else f"{parsed.scheme}://"
    )


@router.get("/authorization-request", response_model=AuthorizationRequestView)
def authorization_request(
    client_id: str = Query(...),
    redirect_uri: str = Query(...),
    code_challenge: str = Query(...),
    db: Session = Depends(get_db),
    user: User = Depends(get_current_user),
):
    try:
        client = oauth_service.check_authorization_request(
            db,
            client_id=client_id,
            redirect_uri=redirect_uri,
            code_challenge=code_challenge,
        )
    except OAuthError as exc:
        raise ApiError(422, "invalid_authorization_request", exc.description) from exc
    return AuthorizationRequestView(
        client_name=oauth_service.client_display_name(client),
        redirect_host=_redirect_host(redirect_uri),
        mcp_enabled=api_token_service.mcp_enabled(db),
    )


@router.post("/authorize", response_model=AuthorizeResult)
def authorize(
    body: AuthorizeDecision,
    db: Session = Depends(get_db),
    user: User = Depends(get_current_user),
):
    try:
        client = oauth_service.check_authorization_request(
            db,
            client_id=body.client_id,
            redirect_uri=body.redirect_uri,
            code_challenge=body.code_challenge,
        )
    except OAuthError as exc:
        raise ApiError(422, "invalid_authorization_request", exc.description) from exc
    if body.decision == "deny":
        return AuthorizeResult(
            redirect_url=construct_redirect_uri(
                body.redirect_uri, error="access_denied", state=body.state
            )
        )
    if not api_token_service.mcp_enabled(db):
        raise ApiError(
            409,
            "mcp_disabled",
            "An admin has switched MCP access off for this instance.",
        )
    try:
        code = oauth_service.create_code(
            db,
            user,
            client,
            redirect_uri=body.redirect_uri,
            redirect_uri_provided_explicitly=body.redirect_uri_provided_explicitly,
            code_challenge=body.code_challenge,
            resource=body.resource,
            access=body.access,
            all_cases=body.all_cases,
            case_ids=body.case_ids,
        )
    except OAuthError as exc:
        raise ApiError(422, "invalid_scope", exc.description) from exc
    db.commit()
    return AuthorizeResult(
        redirect_url=construct_redirect_uri(
            body.redirect_uri, code=code, state=body.state
        )
    )
