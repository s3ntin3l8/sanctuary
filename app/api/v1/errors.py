"""Uniform JSON errors for ``/api/v1``: ``{"detail": ..., "code": ...}``."""

from __future__ import annotations

from http import HTTPStatus
from typing import Any

from fastapi import HTTPException
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse

from app.schemas.common import ErrorResponse

ERROR_RESPONSES: dict[int | str, dict[str, Any]] = {
    "default": {"model": ErrorResponse, "description": "Error"}
}


class ApiError(HTTPException):
    """An ``/api/v1`` failure carrying a stable machine-readable ``code``."""

    def __init__(self, status_code: int, code: str, detail: str) -> None:
        super().__init__(status_code=status_code, detail=detail)
        self.code = code


def is_api_v1_path(path: str) -> bool:
    return path.startswith("/api/v1/")


def is_api_path(path: str) -> bool:
    """Any JSON API (v1 plus the OAuth/ingest helpers): errors use the uniform envelope."""
    return path.startswith("/api/")


def _status_code_name(status_code: int) -> str:
    try:
        return HTTPStatus(status_code).name.lower()
    except ValueError:
        return "error"


def error_response(status_code: int, code: str, detail: str) -> JSONResponse:
    return JSONResponse({"detail": detail, "code": code}, status_code=status_code)


def http_error_response(exc: Exception, *, default_status: int) -> JSONResponse:
    """Render any exception raised under ``/api/v1`` in the uniform shape.

    Non-HTTP exceptions (an unhandled 500) never leak their message.
    """
    if isinstance(exc, ApiError):
        return error_response(exc.status_code, exc.code, str(exc.detail))
    status_code = getattr(exc, "status_code", default_status)
    if hasattr(exc, "detail") and status_code < 500:
        detail = str(exc.detail)
    else:
        detail = HTTPStatus(status_code).phrase
    return error_response(status_code, _status_code_name(status_code), detail)


def validation_error_response(exc: RequestValidationError) -> JSONResponse:
    first = exc.errors()[0] if exc.errors() else {}
    field = ".".join(str(p) for p in first.get("loc", ())[1:])
    message = first.get("msg", "Invalid request")
    detail = f"{field}: {message}" if field else message
    return error_response(422, "validation_error", detail)
