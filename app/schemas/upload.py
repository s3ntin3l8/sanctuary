"""Document upload results."""

from __future__ import annotations

from typing import Literal

from pydantic import BaseModel


class UploadResult(BaseModel):
    filename: str
    status: Literal["queued", "duplicate", "error"]
    doc_id: int | None = None
    batch_id: int | None = None
    message: str | None = None


class UploadResponse(BaseModel):
    results: list[UploadResult]
    queued: int
    failed: int
    batch_id: int | None


class UploadTarget(BaseModel):
    case_id: str | None
    case_title: str | None
    parent_options: list[dict[str, str | int]]
