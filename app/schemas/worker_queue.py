"""The document-processing queue (what the Celery workers are doing)."""

from __future__ import annotations

from datetime import datetime
from typing import Literal

from pydantic import BaseModel

from app.models.enums import PipelineStage
from app.schemas.gmail_import import GmailImportStatus


class QueueItem(BaseModel):
    kind: Literal["doc", "batch", "slicing"]
    # None for a scan awaiting slicing: it isn't in the document pipeline yet.
    stage: PipelineStage | None
    label: str
    doc_id: int | None
    batch_id: int | None
    doc_count: int
    # Why a queued item isn't running yet, when that is deliberate.
    note: str | None = None
    # From the stage row: when it started (executing) or its retry schedule.
    started_at: datetime | None = None
    attempt: int | None = None
    max_attempts: int | None = None
    next_at: datetime | None = None


class FailedStage(BaseModel):
    stage: str
    error: str
    attempt: int | None = None


class FailedDoc(BaseModel):
    doc_id: int
    batch_id: int | None
    title: str
    # Every failed stage, root cause first (downstream ones often just say
    # "upstream X failed").
    stages: list[FailedStage]


class RecentStage(BaseModel):
    doc_id: int
    batch_id: int | None
    title: str
    stage: str
    completed_at: datetime
    duration_ms: int | None


class WorkerStatus(BaseModel):
    name: str
    up: bool
    concurrency: int


class QueueDepth(BaseModel):
    ingest: int
    ai: int
    maintenance: int


class GateStatus(BaseModel):
    # Model families currently holding the GPU gate (chandra / qwen / embed).
    holders: list[str]
    waiting: int


class SystemStatus(BaseModel):
    """Each part is None when it can't be read (Redis or the workers are down)."""

    workers: list[WorkerStatus] | None
    queue_depth: QueueDepth | None
    gpu_gate: GateStatus | None


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
    recent: list[RecentStage]
    # Labels of the AI calls in flight right now, e.g. "enrich:doc:42".
    ai_calls: list[str]
    system: SystemStatus
    # The user's Gmail import while it runs (and briefly after), else None.
    gmail_import: GmailImportStatus | None = None
