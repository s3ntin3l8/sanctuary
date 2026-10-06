"""Case cards, the cases directory and case creation."""

from __future__ import annotations

from datetime import datetime

from pydantic import BaseModel, Field, field_validator

from app.models.enums import (
    ActionItemType,
    CaseStatus,
    Jurisdiction,
    SignificanceTier,
)


class NextAction(BaseModel):
    title: str
    due_date: datetime | None
    action_type: ActionItemType


class CaseCard(BaseModel):
    """One case as shown on Home and in the Cases directory."""

    id: str
    title: str
    status: CaseStatus
    status_label: str
    is_draft: bool
    pending_close: bool
    client_name: str
    opposing_party: str
    proceeding_name: str
    matter_type: str
    next_action: NextAction | None
    exposure_eur: float
    doc_count: int
    open_action_count: int
    new_docs: int
    days_since_activity: int
    is_dormant: bool
    max_significance: SignificanceTier | None
    last_activity_at: datetime | None


class CasesDirectory(BaseModel):
    cases: list[CaseCard]
    counts_by_status: dict[CaseStatus, int]
    total: int


class CaseCreate(BaseModel):
    # The id becomes a URL segment everywhere (/cases/{id}), so keep it URL-safe.
    case_id: str = Field(min_length=1, max_length=64, pattern=r"^[A-Za-z0-9._-]+$")
    title: str = Field(min_length=1, max_length=255)
    court_name: str = Field(min_length=1, max_length=255)
    jurisdiction: Jurisdiction = Jurisdiction.DE

    @field_validator("case_id", "title", "court_name", mode="before")
    @classmethod
    def _strip(cls, value: object) -> object:
        return value.strip() if isinstance(value, str) else value


class CaseCreated(BaseModel):
    id: str
