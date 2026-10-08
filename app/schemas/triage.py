"""The triage inbox: bundles awaiting review, their documents and actions."""

from __future__ import annotations

from datetime import datetime
from typing import Literal

from pydantic import BaseModel, Field, field_validator

from app.models.enums import (
    DocumentRole,
    IngestBatchSourceType,
    OriginatorType,
    PipelineState,
    ProceedingCourtLevel,
    SignificanceTier,
)

BundleStatus = Literal["stuck", "processing", "needs_classification", "needs_review"]
PipelineFilter = Literal["ready", "review_metadata", "processing", "failed"]


class TriageDocument(BaseModel):
    id: int
    title: str
    role: DocumentRole
    depth: int
    originator_type: OriginatorType
    significance_tier: SignificanceTier | None
    pipeline_state: PipelineState
    needs_review: bool
    review_reasons: list[str]
    page_count: int
    is_proof: bool
    sub_group_id: int | None


class TriageSubGroup(BaseModel):
    id: str
    sub_group_id: int | None
    label: str
    lead_doc_id: int | None
    suggested_case_id: str | None
    suggested_case_title: str | None
    case_confidence: str | None
    doc_ids: list[int]


class TriageProceeding(BaseModel):
    id: int
    az_court: str | None
    court_name: str
    court_level: ProceedingCourtLevel


class TriageCaseSuggestion(BaseModel):
    case_id: str
    title: str | None
    is_draft: bool
    exists: bool


class TriageActionDate(BaseModel):
    title: str
    due_date: datetime | None


class BundlePipeline(BaseModel):
    total: int
    counts: dict[PipelineState, int]
    active_label: str | None
    failed_error: str | None


class TriageBundle(BaseModel):
    key: str
    batch_id: int | None
    is_synthetic: bool
    source_type: IngestBatchSourceType
    subject: str | None
    sender_email: str | None
    received_at: datetime
    status: BundleStatus
    pipeline_filter: PipelineFilter
    pipeline: BundlePipeline
    confirmed_case_id: str | None
    suggestion: TriageCaseSuggestion | None
    proceeding: TriageProceeding | None
    doc_count: int
    total_pages: int
    originator_types: list[OriginatorType]
    action_dates: list[TriageActionDate]
    has_manual_groups: bool
    has_unconfirmed_metadata: bool
    unresolved_review_count: int
    to_confirm_count: int
    lead_doc_id: int | None
    documents: list[TriageDocument]
    sub_groups: list[TriageSubGroup]


class TriageStats(BaseModel):
    pending: int
    needs_classification: int
    needs_review: int
    stuck: int
    processing: int
    failed_docs: int
    first_failed_doc_id: int | None
    drafts_pending: int
    first_draft_doc_id: int | None


class PickerOption(BaseModel):
    value: str
    label: str


class PickerCase(BaseModel):
    id: str
    title: str


class PickerProceeding(BaseModel):
    id: int
    case_id: str
    label: str


class SlicingQueueItem(BaseModel):
    batch_id: int
    subject: str | None
    page_count: int | None
    status: str


class TriageView(BaseModel):
    bundles: list[TriageBundle]
    stats: TriageStats
    filter_options: dict[str, list[PickerOption]]
    cases: list[PickerCase]
    proceedings: list[PickerProceeding]
    slicing_queue: list[SlicingQueueItem]


class RetryAllResult(BaseModel):
    retried: int


class TriageConfirm(BaseModel):
    """Route a bundle (or loose document) to a case.

    ``confirm_bundle`` finalises and removes it from triage; ``assign_case``
    keeps it in the inbox for further per-document review.
    """

    batch_id: int | None = None
    doc_id: int | None = None
    action: Literal["confirm_bundle", "assign_case"] = "confirm_bundle"
    case_id: str | None = None
    new_case_id: str | None = Field(default=None, max_length=64)
    new_case_title: str | None = Field(default=None, max_length=255)
    proceeding_id: int | None = None


class ConfirmedCase(BaseModel):
    id: str
    title: str
    action: Literal["created", "assigned", "ratified"]


class TriageConfirmResult(BaseModel):
    bundle: TriageBundle | None
    next_doc_id: int | None
    case: ConfirmedCase


class BatchKeys(BaseModel):
    keys: list[str] = Field(min_length=1)


class BatchAssign(BatchKeys):
    case_id: str | None = None
    new_case_id: str | None = Field(default=None, max_length=64)
    new_case_title: str | None = Field(default=None, max_length=255)
    proceeding_id: int | None = None


class BatchResult(BaseModel):
    confirmed: int
    skipped: int
    bundles: list[TriageBundle]
    removed_keys: list[str]


class TitleUpdate(BaseModel):
    title: str = Field(max_length=500)

    @field_validator("title")
    @classmethod
    def _strip_non_empty(cls, value: str) -> str:
        value = value.strip()
        if not value:
            raise ValueError("Title must not be empty")
        return value


class GroupRename(BaseModel):
    label: str = ""
    lead_doc_id: int | None = None


class GroupTarget(BaseModel):
    lead_doc_id: int | None = None


class GroupOrder(BaseModel):
    doc_ids: list[int] = Field(min_length=1)
    lead_doc_id: int | None = None


class CoverUpdate(BaseModel):
    doc_id: int
