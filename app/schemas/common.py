"""Shapes shared by every ``/api/v1`` endpoint."""

from __future__ import annotations

from pydantic import BaseModel


class ErrorResponse(BaseModel):
    """Body of every non-2xx ``/api/v1`` response.

    ``code`` is a stable machine-readable identifier the client branches on;
    ``detail`` is the human-readable message it may show as-is.
    """

    detail: str
    code: str
