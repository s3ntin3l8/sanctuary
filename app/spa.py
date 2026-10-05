"""Serving the built single-page app (``frontend/``).

Vite emits ``index.html`` plus content-hashed files under ``assets/``. Routes
that have been cut over to the SPA return :func:`spa_index`; the client-side
router takes it from there.
"""

from __future__ import annotations

from fastapi.responses import HTMLResponse, PlainTextResponse, Response
from starlette.staticfiles import StaticFiles
from starlette.types import Scope

from app import config


class ImmutableStaticFiles(StaticFiles):
    """Static files whose names contain a content hash: cache them forever."""

    async def get_response(self, path: str, scope: Scope) -> Response:
        response = await super().get_response(path, scope)
        if response.status_code == 200:
            response.headers["Cache-Control"] = "public, max-age=31536000, immutable"
        return response


def spa_index() -> Response:
    """Return the SPA entry document (never cached: it names the hashed assets)."""
    index = config.FRONTEND_DIST / "index.html"
    try:
        html = index.read_text(encoding="utf-8")
    except FileNotFoundError:
        return PlainTextResponse(
            "The frontend bundle has not been built. Run `make frontend-build`.",
            status_code=503,
        )
    return HTMLResponse(html, headers={"Cache-Control": "no-cache"})
