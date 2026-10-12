"""OAuth 2.1 authorization server for MCP clients (Claude Desktop, claude.ai, …).

The SDK's request handlers for ``/authorize``, ``/token``, ``/register`` and
``/revoke`` are reused as-is, backed by ``SanctuaryOAuthProvider`` (tables in
``app/services/oauth_service``). They are mounted on the main app rather than
through ``MCPServer(auth=…)`` because that wiring fixes the issuer URL at
construction and adds its own bearer middleware; here the single bearer check
stays in ``AuthGateMiddleware`` and the metadata is built per request from the
URL the client actually reached.
"""

from __future__ import annotations

from collections.abc import Callable
from typing import Any
from urllib.parse import urlencode

import anyio.to_thread
from mcp.server.auth.handlers.authorize import AuthorizationHandler
from mcp.server.auth.handlers.register import RegistrationHandler
from mcp.server.auth.handlers.token import TokenHandler
from mcp.server.auth.json_response import PydanticJSONResponse
from mcp.server.auth.middleware.client_auth import ClientAuthenticator
from mcp.server.auth.provider import (
    AuthorizationCode,
    AuthorizationParams,
    OAuthAuthorizationServerProvider,
    RefreshToken,
    RegistrationError,
    TokenError,
)
from mcp.server.auth.settings import ClientRegistrationOptions
from mcp.server.transport_security import (
    DEFAULT_MAX_REQUEST_BODY_SIZE,
    RequestBodyLimitMiddleware,
)
from mcp.shared.auth import (
    OAuthClientInformationFull,
    OAuthMetadata,
    OAuthToken,
    ProtectedResourceMetadata,
)
from mcp.shared.inbound import MCP_PROTOCOL_VERSION_HEADER
from pydantic import AnyHttpUrl, AnyUrl
from sqlalchemy.orm import Session
from starlette.middleware.cors import CORSMiddleware
from starlette.requests import Request
from starlette.responses import JSONResponse, Response
from starlette.routing import Route, request_response
from starlette.types import ASGIApp, Receive, Scope, Send

from app import dependencies
from app.core.rate_limit import (
    OAUTH_REGISTER_LIMIT,
    OAUTH_TOKEN_LIMIT,
    exempt_asgi,
    hit_ip_limit,
)
from app.services import api_token_service, oauth_service
from app.services.oauth_service import OAuthError

CONSENT_PATH = "/oauth/authorize"
RESOURCE_PATH = "/mcp"
RESOURCE_METADATA_PATH = f"/.well-known/oauth-protected-resource{RESOURCE_PATH}"


async def _in_session[T](fn: Callable[[Session], T]) -> T:
    """Run ``fn`` on a worker thread with its own session (committed on success)."""

    def work() -> T:
        db = dependencies.SessionLocal()
        try:
            result = fn(db)
            db.commit()
            return result
        except BaseException:
            db.rollback()
            raise
        finally:
            db.close()

    return await anyio.to_thread.run_sync(work)


def _timestamp(value) -> int:
    return int(value.timestamp())


class SanctuaryOAuthProvider(
    OAuthAuthorizationServerProvider[AuthorizationCode, RefreshToken, Any]
):
    """The SDK's ``OAuthAuthorizationServerProvider``, backed by Postgres."""

    async def get_client(self, client_id: str) -> OAuthClientInformationFull | None:
        return await _in_session(lambda db: oauth_service.get_client(db, client_id))

    async def register_client(self, client_info: OAuthClientInformationFull) -> None:
        def work(db: Session) -> None:
            try:
                oauth_service.register_client(db, client_info)
            except OAuthError as exc:
                raise RegistrationError(exc.error, exc.description) from exc  # type: ignore[arg-type]

        await _in_session(work)

    async def authorize(
        self, client: OAuthClientInformationFull, params: AuthorizationParams
    ) -> str:
        """Send the browser to the consent page (an SPA route behind the login)."""
        query = {
            "client_id": client.client_id,
            "redirect_uri": str(params.redirect_uri),
            "explicit": "1" if params.redirect_uri_provided_explicitly else "0",
            "code_challenge": params.code_challenge,
        }
        if params.state is not None:
            query["state"] = params.state
        if params.resource is not None:
            query["resource"] = params.resource
        return f"{CONSENT_PATH}?{urlencode(query)}"

    async def load_authorization_code(
        self, client: OAuthClientInformationFull, authorization_code: str
    ) -> AuthorizationCode | None:
        def work(db: Session) -> AuthorizationCode | None:
            row = oauth_service.load_code(db, client.client_id, authorization_code)
            if row is None:
                return None
            return AuthorizationCode(
                code=authorization_code,
                scopes=[],
                expires_at=row.expires_at.timestamp(),
                client_id=row.client_id,
                code_challenge=row.code_challenge,
                redirect_uri=AnyUrl(row.redirect_uri),
                redirect_uri_provided_explicitly=row.redirect_uri_provided_explicitly,
                resource=row.resource,
            )

        return await _in_session(work)

    async def exchange_authorization_code(
        self, client: OAuthClientInformationFull, authorization_code: AuthorizationCode
    ) -> OAuthToken:
        def work(db: Session) -> OAuthToken:
            row = oauth_service.load_code(db, client.client_id, authorization_code.code)
            if row is None:
                raise TokenError("invalid_grant", "Unknown authorization code.")
            try:
                access, refresh, expires_in = oauth_service.redeem_code(db, row)
            except OAuthError as exc:
                raise TokenError("invalid_grant", exc.description) from exc
            return OAuthToken(
                access_token=access, expires_in=expires_in, refresh_token=refresh
            )

        return await _in_session(work)

    async def load_refresh_token(
        self, client: OAuthClientInformationFull, refresh_token: str
    ) -> RefreshToken | None:
        def work(db: Session) -> RefreshToken | None:
            grant = oauth_service.load_refresh(db, client.client_id, refresh_token)
            if grant is None:
                return None
            return RefreshToken(
                token=refresh_token,
                client_id=client.client_id,
                scopes=[],
                expires_at=_timestamp(grant.refresh_expires_at),
                resource=grant.resource,
            )

        return await _in_session(work)

    async def exchange_refresh_token(
        self,
        client: OAuthClientInformationFull,
        refresh_token: RefreshToken,
        scopes: list[str],
    ) -> OAuthToken:
        def work(db: Session) -> OAuthToken:
            grant = oauth_service.load_refresh(
                db, client.client_id, refresh_token.token
            )
            if grant is None:
                raise TokenError(
                    "invalid_grant", "The refresh token is no longer valid."
                )
            access, refresh, expires_in = oauth_service.refresh_grant(db, grant)
            return OAuthToken(
                access_token=access, expires_in=expires_in, refresh_token=refresh
            )

        return await _in_session(work)


# The protocol also lists load_access_token/revoke_token; the SDK handlers we mount
# never call them (bearer checks live in AuthGateMiddleware, revoke is _revoke below).
provider = SanctuaryOAuthProvider()  # type: ignore[abstract]


# --- Endpoints -------------------------------------------------------------------


def base_url(request: Request) -> str:
    return str(request.base_url).rstrip("/")


class _Guarded:
    """503 while MCP is switched off; optionally rate limit POSTs per client IP.

    A class, not a function: Starlette treats a bare function endpoint as a
    request handler, but an object with ``__call__`` as an ASGI app.
    """

    def __init__(self, app: ASGIApp, *, limit=None, name: str = "") -> None:
        self.app = app
        self.limit = limit
        self.name = name

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] == "http":
            if not await _in_session(api_token_service.mcp_enabled):
                await JSONResponse(
                    {
                        "detail": "MCP access is disabled on this instance.",
                        "code": "mcp_disabled",
                    },
                    status_code=503,
                )(scope, receive, send)
                return
            client = scope.get("client")
            if (
                self.limit is not None
                and scope["method"] == "POST"
                and not hit_ip_limit(
                    self.name, self.limit, client[0] if client else "unknown"
                )
            ):
                await JSONResponse(
                    {
                        "error": "temporarily_unavailable",
                        "error_description": "Too many requests.",
                    },
                    status_code=429,
                    headers={"Retry-After": "60"},
                )(scope, receive, send)
                return
        await self.app(scope, receive, send)


def _cors(app: ASGIApp, methods: list[str]) -> ASGIApp:
    return CORSMiddleware(
        app,
        allow_origins="*",
        allow_methods=methods,
        allow_headers=[MCP_PROTOCOL_VERSION_HEADER],
    )


def _handler(handle, *, methods: list[str], cors: bool, limit=None, name="") -> ASGIApp:
    app: ASGIApp = RequestBodyLimitMiddleware(
        request_response(handle), DEFAULT_MAX_REQUEST_BODY_SIZE
    )
    app = _Guarded(app, limit=limit, name=name)
    app = _cors(app, methods) if cors else app
    return exempt_asgi(app, f"oauth-{name or handle.__name__}")


async def _authorization_server_metadata(request: Request) -> Response:
    base = base_url(request)
    return PydanticJSONResponse(
        OAuthMetadata(
            issuer=AnyHttpUrl(base),
            authorization_endpoint=AnyHttpUrl(f"{base}/authorize"),
            token_endpoint=AnyHttpUrl(f"{base}/token"),
            registration_endpoint=AnyHttpUrl(f"{base}/register"),
            revocation_endpoint=AnyHttpUrl(f"{base}/revoke"),
            response_types_supported=["code"],
            grant_types_supported=["authorization_code", "refresh_token"],
            # Public clients only; PKCE S256 is mandatory.
            token_endpoint_auth_methods_supported=["none"],
            revocation_endpoint_auth_methods_supported=["none"],
            code_challenge_methods_supported=["S256"],
        ),
        headers={"Cache-Control": "public, max-age=300"},
    )


async def _protected_resource_metadata(request: Request) -> Response:
    base = base_url(request)
    return PydanticJSONResponse(
        ProtectedResourceMetadata(
            resource=AnyHttpUrl(f"{base}{RESOURCE_PATH}"),
            authorization_servers=[AnyHttpUrl(base)],
            resource_name="Sanctuary",
        ),
        headers={"Cache-Control": "public, max-age=300"},
    )


async def _revoke(request: Request) -> Response:
    """RFC 7009 for public clients: an access or refresh token ends its grant.

    The SDK's revocation handler insists on a ``client_secret`` form field even
    for public clients, so a standard request would be rejected; a token is only
    revoked by the client it was issued to.
    """
    form = await request.form()
    client_id, secret = form.get("client_id"), form.get("token")
    if not isinstance(client_id, str) or not isinstance(secret, str):
        return JSONResponse({"error": "invalid_request"}, status_code=400)

    def work(db: Session) -> None:
        token = oauth_service.grant_for_secret(db, secret)
        if token is not None and token.oauth_client_id == client_id:
            api_token_service.revoke(
                db, token, actor_user_id=token.user_id, reason="client_revoked"
            )

    await _in_session(work)
    return Response(status_code=200)  # also for unknown tokens, per the RFC


def oauth_routes() -> list[Route]:
    authenticator = ClientAuthenticator(provider)
    registration = RegistrationHandler(
        provider, options=ClientRegistrationOptions(enabled=True)
    )
    token = TokenHandler(provider, authenticator)
    get = ["GET", "OPTIONS"]
    post = ["POST", "OPTIONS"]
    return [
        Route(
            "/.well-known/oauth-authorization-server",
            _handler(_authorization_server_metadata, methods=get, cors=True),
            methods=get,
        ),
        Route(
            RESOURCE_METADATA_PATH,
            _handler(_protected_resource_metadata, methods=get, cors=True),
            methods=get,
        ),
        # Some clients probe the path-less form before the path-inserted one.
        Route(
            "/.well-known/oauth-protected-resource",
            _handler(_protected_resource_metadata, methods=get, cors=True),
            methods=get,
        ),
        # No CORS on /authorize: clients navigate the browser there.
        Route(
            "/authorize",
            _handler(
                AuthorizationHandler(provider).handle,
                methods=["GET", "POST"],
                cors=False,
            ),
            methods=["GET", "POST"],
        ),
        Route(
            "/register",
            _handler(
                registration.handle,
                methods=post,
                cors=True,
                limit=OAUTH_REGISTER_LIMIT,
                name="oauth-register",
            ),
            methods=post,
        ),
        Route(
            "/token",
            _handler(
                token.handle,
                methods=post,
                cors=True,
                limit=OAUTH_TOKEN_LIMIT,
                name="oauth-token",
            ),
            methods=post,
        ),
        Route(
            "/revoke",
            _handler(
                _revoke,
                methods=post,
                cors=True,
                limit=OAUTH_TOKEN_LIMIT,
                name="oauth-revoke",
            ),
            methods=post,
        ),
    ]
