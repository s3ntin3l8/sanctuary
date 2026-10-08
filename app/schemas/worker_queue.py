"""The document-processing queue (what the Celery workers are doing)."""

from __future__ import annotations

from typing import Literal

from pydantic import BaseModel

from app.models.enums import PipelineStage
from app.schemas.gmail_import import GmailImportStatus


class QueueItem(BaseModel):
    kind: Literal["doc", "batch"]
    stage: PipelineStage
    label: str
    doc_id: int | None
    batch_id: int | None
    doc_count: int
    # Why a queued item isn't running yet, when that is deliberate.
    note: str | None = None


class FailedDoc(BaseModel):
    doc_id: int
    batch_id: int | None
    title: str
    stage: str
    error: str


class QueueCounts(BaseModel):
    executing: int
    queued: int
    failed: int
    ai_inflight: int


class QueueView(BaseModel):
    counts: QueueCounts
    executing: list[QueueItem]
    queued: list[QueueItem]
    failed: list[FailedDoc]
    # The user's Gmail import while it runs (and briefly after), else None.
    gmail_import: GmailImportStatus | None = None
