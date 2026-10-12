"""Run a synchronous tool body on a worker thread with its own DB session."""

from __future__ import annotations

from collections.abc import Callable
from typing import Any

import anyio.to_thread
from mcp.server.mcpserver import Context
from mcp.server.mcpserver.exceptions import ToolError
from sqlalchemy.orm import Session

from app import dependencies
from app.mcp.principal import McpPrincipal


def _token_id(ctx: Context[Any, Any]) -> int | None:
    """The token id the auth gate stored on the ASGI scope for this request."""
    request = ctx.request_context.request
    scope = getattr(request, "scope", None) or {}
    return scope.get("state", {}).get("api_token_id")


async def run[T](ctx: Context[Any, Any], fn: Callable[[Session, McpPrincipal], T]) -> T:
    """Call ``fn(db, principal)`` off the event loop; commit on success, else roll back."""
    token_id = _token_id(ctx)
    if token_id is None:
        raise ToolError("Not authenticated.")

    def work() -> T:
        db = dependencies.SessionLocal()
        try:
            result = fn(db, McpPrincipal.load(db, token_id=token_id))
            db.commit()
            return result
        except BaseException:
            db.rollback()
            raise
        finally:
            db.close()

    return await anyio.to_thread.run_sync(work)
