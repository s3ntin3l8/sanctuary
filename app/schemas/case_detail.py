"""The case dashboard: detail, graph, timeline, truth map, financials, sharing."""

from __future__ import annotations

from datetime import datetime
from typing import Literal

from pydantic import BaseModel, Field, field_validator

from app.models.enums import (
    ActionItemStatus,
    ActionItemType,
    CaseAccessLevel,
    CaseStatus,
    CaseType,
    ClaimEvidenceRole,
    ClaimStatus,
    ClaimType,
    CostCategory,
    CostSignalType,
    CostStatus,
    DocumentType,
    OriginatorType,
    ProceedingCourtLevel,
    ProceedingStatus,
    RelationshipConfidence,
    SignificanceTier,
    UserReactionType,
)

SignificanceFilter = Literal["critical", "significant+", "all"]
TruthMapFilter = Literal["open", "established", "refuted", "all"]


# --- Detail ------------------------------------------------------------------


class ProceedingView(BaseModel):
    id: int
    court_name: str
    court_level: ProceedingCourtLevel
    az_court: str | None
    subject_matter: str | None
    status: ProceedingStatus
    is_draft: bool
    doc_count: int
    is_deletable: bool


class PartyView(BaseModel):
    name: str
    role: str
    document_count: int = 0


BriefStatus = Literal["ready", "processing", "failed", "none"]


class BriefView(BaseModel):
    """The last good `Case.ai_brief` (content fields stay filled while a refresh
    is `processing` or has `failed`) plus the regeneration job state."""

    status: BriefStatus
    error: str | None = None
    posture: str | None = None
    pressure_points: list[str] = Field(default_factory=list)
    next_move: str | None = None
    detected_status: str | None = None
    status_rationale: str | None = None
    updated_at: datetime | None = None


class CaseActionItem(BaseModel):
    id: int
    title: str
    description: str | None
    due_date: datetime
    action_type: ActionItemType
    status: ActionItemStatus
    location: str | None
    addressee: str | None
    proceeding_id: int | None
    source_document_id: int | None
    is_overdue: bool


class CaseDocument(BaseModel):
    """One row of the case spine (documents of the active proceeding)."""

    id: int
    title: str
    originator_type: OriginatorType
    document_type: DocumentType | None
    issued_date: datetime | None
    received_date: datetime | None
    significance_tier: SignificanceTier | None
    role: str
    thread_open: bool
    needs_review: bool
    summary_pending: bool = False
    is_new: bool


class FinancialsSummary(BaseModel):
    total_cost_exposure_cents: int
    booked: float
    paid: float
    outstanding: float
    reimbursable: float


class CaseDetail(BaseModel):
    id: str
    title: str
    status: CaseStatus
    case_type: CaseType
    jurisdiction: str
    is_draft: bool
    pending_close: bool
    close_suggestion_rationale: str | None
    assume_worst_case: bool
    can_edit: bool
    can_manage_sharing: bool
    proceedings: list[ProceedingView]
    active_proceeding_id: int | None
    documents: list[CaseDocument]
    new_doc_count: int
    last_visit: datetime | None
    action_items: list[CaseActionItem]
    parties: list[PartyView]
    opposing_parties: list[str]
    brief: BriefView
    financials: FinancialsSummary
    open_claim_count: int
    dormancy_alert: str | None


class CaseUpdate(BaseModel):
    title: str | None = Field(default=None, max_length=500)
    status: CaseStatus | None = None
    case_type: CaseType | None = None
    assume_worst_case: bool | None = None

    @field_validator("title")
    @classmethod
    def _strip(cls, value: str | None) -> str | None:
        if value is None:
            return None
        value = value.strip()
        if not value:
            raise ValueError("Title must not be empty")
        return value


class CasePurge(BaseModel):
    confirm: str


class OpposingPartiesUpdate(BaseModel):
    opposing_parties: list[str]

    @field_validator("opposing_parties")
    @classmethod
    def _clean(cls, value: list[str]) -> list[str]:
        return [v.strip() for v in value if v.strip()]


class ReenrichResult(BaseModel):
    queued: int


class ProceedingUpdate(BaseModel):
    court_name: str | None = Field(default=None, max_length=200)
    court_level: ProceedingCourtLevel | None = None
    az_court: str | None = Field(default=None, max_length=100)
    subject_matter: str | None = Field(default=None, max_length=500)
    status: ProceedingStatus | None = None


# --- Graph -------------------------------------------------------------------


class GraphLane(BaseModel):
    key: str
    label: str
    color: str


class GraphNode(BaseModel):
    id: int
    lane: str
    row: int
    x: int
    y: int
    w: int
    h: int
    title: str
    full_title: str
    role: str
    date_short: str
    tier: str | None
    thread_open: bool
    ghost: bool
    cross_proceeding: bool
    proceeding_label: str | None
    is_bundle: bool
    is_new_since_last_visit: bool
    reaction: str | None
    originator_type: str


class GraphBundleChild(BaseModel):
    id: int
    title: str
    origin: str
    date: str
    tier: str | None


class GraphBundle(BaseModel):
    id: int
    lane: str
    originator: str
    row: int
    x: int
    y: int
    header: str
    footer: str
    children: list[GraphBundleChild]


class GraphEdge(BaseModel):
    id: str
    from_node_id: int
    to_node_id: int
    kind: str
    path: str
    dashed: bool
    dasharray: str | None
    stroke_w: float
    arrow: bool


class GraphView(BaseModel):
    proceeding_id: int
    filter: SignificanceFilter
    lanes: list[GraphLane]
    nodes: list[GraphNode]
    bundles: list[GraphBundle]
    edges: list[GraphEdge]
    proof_badges: dict[str, int]
    svg_width: int
    svg_height: int
    node_counts: dict[str, int]
    node_count: int
    edge_count: int


# --- Timeline ----------------------------------------------------------------


class TimelineEventView(BaseModel):
    id: str
    date: datetime
    actor: str
    kind: str
    title: str
    sig: str | None
    source_document_id: int | None
    note: str | None
    amount_eur: float | None
    direction: str | None
    is_overdue: bool
    is_future: bool
    claim_count: int
    quiet_gap_days: int | None


class MonthBucket(BaseModel):
    key: str
    label: str
    total: int
    critical: int
    future: int
    max_total: int


class TimelineView(BaseModel):
    today: datetime
    total_count: int
    events: list[TimelineEventView]
    month_buckets: list[MonthBucket]


# --- Truth map ---------------------------------------------------------------


class EvidenceView(BaseModel):
    id: int
    role: ClaimEvidenceRole
    excerpt: str | None
    confidence: RelationshipConfidence
    document_id: int
    document_title: str
    document_originator: OriginatorType
    document_date: datetime | None
    reactions: list[UserReactionType]


class ClaimView(BaseModel):
    id: int
    claim_text: str
    claim_type: ClaimType
    status: ClaimStatus
    is_precedent: bool
    first_made_at: datetime
    last_updated_at: datetime
    allowed_transitions: list[ClaimStatus]
    evidence: list[EvidenceView]


class ClaimGroupView(BaseModel):
    status: ClaimStatus
    claims: list[ClaimView]


class MergeProposalView(BaseModel):
    proposal_id: int
    confidence: str
    rationale: str | None
    new_claim_id: int
    new_claim_text: str
    existing_claim_id: int
    existing_claim_text: str


class EvidenceProposalView(BaseModel):
    proposal_id: int
    proposed_role: ClaimEvidenceRole
    excerpt: str | None
    target_claim_id: int
    target_claim_text: str
    target_claim_status: ClaimStatus
    source_document_id: int
    source_document_title: str | None


class DedupJob(BaseModel):
    status: Literal["running", "done", "failed"]
    processed: int = 0
    total: int = 0
    proposals: int = 0
    error: str | None = None


class TruthMapView(BaseModel):
    filter: TruthMapFilter
    groups: list[ClaimGroupView]
    open_claim_count: int
    pending_merges: list[MergeProposalView]
    pending_evidence: list[EvidenceProposalView]
    pipeline_active_doc_count: int
    dedup_job: DedupJob | None


class ClaimStatusUpdate(BaseModel):
    status: ClaimStatus


class ProposalBatch(BaseModel):
    action: Literal["confirm", "dismiss"]


class ProposalBatchResult(BaseModel):
    confirmed: int
    dismissed: int


# --- Financials --------------------------------------------------------------


class CostRow(BaseModel):
    id: int
    case_id: str
    proceeding_id: int | None
    title: str
    category: CostCategory
    status: CostStatus
    rvg_position: str | None
    amount_net: float
    vat_rate: float | None
    amount_gross: float
    amount_paid: float | None
    amount_reimbursed: float | None
    is_reimbursable: bool | None
    issued_at: datetime | None
    due_at: datetime | None
    paid_at: datetime | None
    streitwert: float | None
    gebuehren_faktor: float | None
    notes: str | None
    auto_created: bool
    source_document_id: int | None


class InstanceExposure(BaseModel):
    proceeding_id: int
    court_name: str
    court_level: ProceedingCourtLevel
    az_court: str | None
    streitwert: float | None
    allocation_source: str
    own_lawyer_gross: float
    own_lawyer_source: str
    court_fee: float
    court_fee_share: float
    court_fee_charged: float
    court_fee_source: str
    opposing_gross: float
    opposing_source: str
    subtotal: float
    own_theoretical: float
    court_theoretical: float
    opposing_theoretical: float
    invoices_own: list[CostRow]
    invoices_court: list[CostRow]
    invoices_opposing: list[CostRow]
    invoices_other: list[CostRow]


class CostSignalDoc(BaseModel):
    id: int
    title: str
    issued_date: datetime | None
    originator_type: OriginatorType
    signal_type: CostSignalType
    signal_id: int
    amount: float | None
    description: str | None
    client_role: str | None
    role_source: str | None


class FinancialsView(BaseModel):
    summary: FinancialsSummary
    instances: list[InstanceExposure]
    case_level_costs: list[CostRow]
    signal_docs: list[CostSignalDoc]


class CostFieldUpdate(BaseModel):
    title: str | None = Field(default=None, max_length=300)
    status: CostStatus | None = None
    category: CostCategory | None = None
    amount_net: float | None = Field(default=None, gt=0)
    vat_rate: float | None = Field(default=None, ge=0, le=1)
    amount_paid: float | None = Field(default=None, ge=0)
    amount_reimbursed: float | None = Field(default=None, ge=0)
    streitwert: float | None = Field(default=None, ge=0)
    gebuehren_faktor: float | None = Field(default=None, gt=0)
    issued_at: datetime | None = None
    due_at: datetime | None = None
    notes: str | None = Field(default=None, max_length=4000)
    is_reimbursable: bool | None = None


class CostCreate(BaseModel):
    """A manually booked cost; gross is derived from net and VAT on the server."""

    proceeding_id: int | None = None
    category: CostCategory
    title: str = Field(min_length=1, max_length=300)
    rvg_position: str | None = Field(default=None, max_length=100)
    amount_net: float = Field(gt=0)
    vat_rate: float = Field(default=0.19, ge=0, le=1)
    status: CostStatus = CostStatus.OFFEN
    streitwert: float | None = Field(default=None, ge=0)
    gebuehren_faktor: float | None = Field(default=None, gt=0)
    issued_at: datetime | None = None
    due_at: datetime | None = None
    notes: str | None = Field(default=None, max_length=4000)
    is_reimbursable: bool = True

    @field_validator("title")
    @classmethod
    def _strip(cls, value: str) -> str:
        value = value.strip()
        if not value:
            raise ValueError("Title must not be empty")
        return value


class CostReimburse(BaseModel):
    amount: float | None = Field(default=None, gt=0)


class CostCaseGroup(BaseModel):
    id: str
    title: str
    status: CaseStatus
    can_edit: bool
    summary: FinancialsSummary
    costs: list[CostRow]


class CostAlert(BaseModel):
    cost: CostRow
    case_title: str
    open_amount: float


class CostsOverview(BaseModel):
    """Every cost the caller may see, grouped by case, with the ledger totals."""

    summary: FinancialsSummary
    overdue: list[CostAlert]
    due_soon: list[CostAlert]
    cases: list[CostCaseGroup]


class ClientRoleUpdate(BaseModel):
    role: Literal["winner", "loser", "unset"]


# --- Sharing -----------------------------------------------------------------


class ShareView(BaseModel):
    user_id: int
    email: str
    display_name: str | None
    permission: CaseAccessLevel


class SharingView(BaseModel):
    owner_email: str | None
    shares: list[ShareView]


class ShareCreate(BaseModel):
    email: str = Field(min_length=3, max_length=320)
    permission: CaseAccessLevel
