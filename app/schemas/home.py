"""The Home dashboard."""

from __future__ import annotations

from datetime import date, datetime
from typing import Literal

from pydantic import BaseModel

from app.models.enums import ActionItemType, IngestBatchStatus, SignificanceTier
from app.schemas.cases import CaseCard


class HomeActionItem(BaseModel):
    id: int
    case_id: str
    case_title: str
    title: str
    description: str | None
    due_date: datetime | None
    action_type: ActionItemType


class PipelineCounts(BaseModel):
    total: int
    running: int
    pending: int
    failed: int
    completed: int


class HomeTriageBundle(BaseModel):
    id: int
    status: IngestBatchStatus
    received_at: datetime | None
    sender_email: str | None
    title: str
    doc_count: int
    case_id: str | None
    suggested_case_id: str | None
    pipeline: PipelineCounts


class HomeDeltaCase(BaseModel):
    case_id: str
    case_title: str
    new_doc_count: int
    new_actions: int
    max_significance: SignificanceTier
    doc_titles: list[str]


class HomeSignal(BaseModel):
    id: str
    kind: str
    severity: str
    title: str
    detail: str
    action: str
    link: str


class HomeActivityEvent(BaseModel):
    """One row of the "Recent activity" strip, derived from a timestamp the
    pipeline already writes (document ingested / enriched, stage failed,
    deadline or hearing extracted, cost paid, case closed, brief refreshed,
    case shared)."""

    kind: Literal[
        "document_ingested",
        "document_enriched",
        "pipeline_failed",
        "deadline_extracted",
        "hearing_scheduled",
        "cost_paid",
        "case_closed",
        "brief_refreshed",
        "case_shared",
    ]
    title: str
    detail: str | None
    occurred_at: datetime
    case_id: str | None
    link: str


class BriefingView(BaseModel):
    """Today's locally generated morning briefing for the signed-in user."""

    status: Literal["processing", "ready", "failed"]
    day: date
    generated_at: datetime | None
    model_label: str | None
    summary: str | None
    priorities: list[str]
    error: str | None


class HomeView(BaseModel):
    greeting: str
    user_name: str
    now: datetime
    today_items: list[HomeActionItem]
    triage_bundles: list[HomeTriageBundle]
    last_home_visit: datetime | None
    delta_cases: list[HomeDeltaCase]
    signals: list[HomeSignal]
    activity: list[HomeActivityEvent]
    draft_cases: list[CaseCard]
    active_cases: list[CaseCard]
    caught_up: bool
