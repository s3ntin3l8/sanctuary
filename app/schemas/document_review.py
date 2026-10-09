"""One document as the review panel (triage inline review, later the HUD rail) sees it."""

from __future__ import annotations

from datetime import datetime
from typing import Literal

from pydantic import BaseModel, Field

from app.models.enums import (
    ActionItemStatus,
    ActionItemType,
    ClaimStatus,
    ClaimType,
    CostSignalType,
    DocumentType,
    OriginatorType,
    PipelineStage,
    PipelineState,
    RelationshipConfidence,
    RelationshipType,
    SignificanceTier,
    StageStatus,
    UserReactionType,
)
from app.schemas.case_detail import EvidenceProposalView

Confidence = Literal["high", "medium", "low"]


class StageView(BaseModel):
    key: PipelineStage
    label: str
    icon: str
    status: StageStatus | None
    error: str | None
    attempt: int | None
    max_attempts: int | None
    next_at: str | None
    completed_at: str | None


class PipelineView(BaseModel):
    state: PipelineState
    stages: list[StageView]
    # 1-indexed pages the OCR engine failed on while EXTRACT still completed.
    ocr_page_failures: list[int]


class MetadataField(BaseModel):
    field: str
    label: str
    value: str | None
    confidence: Confidence | None


class CaseRef(BaseModel):
    id: str
    title: str
    is_draft: bool


class ProceedingRef(BaseModel):
    id: int
    case_id: str
    court_name: str
    az_court: str | None
    court_level: str
    is_draft: bool


class SummaryBullet(BaseModel):
    kind: Literal["legal", "action", "finance"]
    text: str


class SummaryView(BaseModel):
    bullets: list[SummaryBullet]
    approved_at: datetime | None
    created_at: datetime | None
    enrich_status: StageStatus | None


class KeyPassage(BaseModel):
    id: str
    text: str
    kind: str | None
    page: int | None
    rationale: str | None
    start_offset: int | None
    end_offset: int | None
    claim_id: int | None
    pin_count: int


class RelationshipView(BaseModel):
    id: int
    doc_id: int
    title: str
    originator_type: OriginatorType
    rel_type: RelationshipType
    confidence: RelationshipConfidence
    direction: Literal["out", "in"]


class GroundView(BaseModel):
    id: int
    claim_text: str
    claim_type: ClaimType
    status: ClaimStatus
    is_precedent: bool
    first_made_at: datetime


class ActionView(BaseModel):
    id: int
    title: str
    description: str | None
    due_date: datetime | None
    action_type: ActionItemType
    status: ActionItemStatus
    location: str | None
    addressee: str | None


class CostSignalView(BaseModel):
    id: int
    signal_type: CostSignalType
    amount: float | None
    description: str | None
    issued_at: datetime | None


class ReactionView(BaseModel):
    reaction: UserReactionType
    notes: str | None
    created_at: datetime | None


class DocumentReview(BaseModel):
    id: int
    title: str
    original_filename: str | None
    page_count: int
    case_id: str | None
    case: CaseRef | None
    proceeding: ProceedingRef | None
    originator_type: OriginatorType
    attributed_originator: str | None
    court_relay: bool
    sender: str | None
    internal_id: str | None
    az_court: str | None
    issued_date: datetime | None
    received_date: datetime | None
    ingest_date: datetime | None
    document_type: DocumentType | None
    significance_tier: SignificanceTier | None
    needs_review: bool
    review_reasons: list[str]
    content_hash: str | None
    metadata: list[MetadataField]
    pipeline: PipelineView
    summary: SummaryView
    key_passages: list[KeyPassage]
    relationships: list[RelationshipView]
    grounds: list[GroundView]
    evidence_proposals: list[EvidenceProposalView]
    contradiction_notes: list[str]
    claims_status: Literal["skipped", "ran", "pending_triage", "pending"]
    actions: list[ActionView]
    cost_signals: list[CostSignalView]
    reactions: list[ReactionView]
    bundle_prev_id: int | None
    bundle_next_id: int | None
    cases: list[CaseRef]
    proceedings: list[ProceedingRef]


class PinView(BaseModel):
    id: int
    passage_id: str
    note: str | None
    user_id: int | None
    updated_at: datetime | None


class PinCreate(BaseModel):
    passage_id: str = Field(min_length=1, max_length=12)
    note: str | None = Field(default=None, max_length=4000)


class PinUpdate(BaseModel):
    note: str | None = Field(default=None, max_length=4000)


class ReaderNav(BaseModel):
    """Where the document sits among its proceeding siblings, bundle and family."""

    prev_doc_id: int | None
    next_doc_id: int | None
    position: int | None
    total: int | None
    parent_id: int | None
    first_child_id: int | None
    bundle_prev_id: int | None
    bundle_next_id: int | None


class DocumentReader(DocumentReview):
    """The full-screen HUD: the review view plus the rendered body and pins."""

    body_html: str | None
    pins: list[PinView]
    nav: ReaderNav
    thread_open: bool
    context_strategy: str | None
    has_original: bool


class MetadataUpdate(BaseModel):
    title: str | None = Field(default=None, max_length=500)
    originator_type: OriginatorType | None = None
    sender: str | None = Field(default=None, max_length=255)
    internal_id: str | None = Field(default=None, max_length=255)
    issued_date: datetime | None = None
    received_date: datetime | None = None
    significance_tier: SignificanceTier | None = None
    document_type: DocumentType | None = None


class SummaryAction(BaseModel):
    action: Literal["approve", "unapprove", "reject"]


class ReactionUpdate(BaseModel):
    reaction: UserReactionType
    notes: str | None = Field(default=None, max_length=4000)


class ActionStatusUpdate(BaseModel):
    status: ActionItemStatus


class CostPromotion(BaseModel):
    """Promote the document's latest cost signal into the ledger."""

    category: str | None = None
    vat_rate: float | None = Field(default=None, ge=0, le=1)
    amount: float | None = Field(default=None, ge=0)


class CostPromoted(BaseModel):
    cost_id: int
    amount_gross: float


class DocumentStatus(BaseModel):
    """Upload-progress polling: where a freshly ingested document is."""

    id: int
    state: PipelineState
    label: str
    error: str | None
