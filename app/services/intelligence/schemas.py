"""Pydantic schemas for grammar-constrained AI output, one per intelligence stage.

These models drive two things:
- the JSON schema we send to the LLM via `response_format` / `format` (server
  enforces the shape grammar-side)
- runtime validation of the parsed response inside `call_json_ai`

Every field name here must match the keys the prompt asks the model to emit.
Enum values must match the prompt's allowed-values lists — the JSON schema
emitted to the model uses these as its `enum` constraint.
"""

from typing import Literal

from pydantic import BaseModel, ConfigDict, Field

from app.models.enums import (
    ClaimEvidenceRole,
    ClaimType,
    DocumentType,
    OriginatorType,
    ProceedingCourtLevel,
    SignificanceTier,
)

_ConfidenceLevel = Literal["high", "medium", "low"]
_ActionType = Literal[
    "deadline",
    "court_date",
    "response_required",
    "filing_required",
    "payment_due",
]
_RelationshipType = Literal["replies_to", "references", "supersedes"]
_EntityType = Literal[
    "person",
    "organization",
    "court",
    "law_firm",
    "citation",
    "financial",
    "legal_category",
]
_CostDeltaKind = Literal[
    "streitwert",
    "cost_ruling",
    "invoice_lawyer",
    "invoice_court",
    "vorschuss_lawyer",
    "vorschuss_court",
    "pkh_grant",
    "pkh_denied",
]
_CostDirection = Literal["incoming", "outgoing", "ruling", "none"]


class ProceedingExtraction(BaseModel):
    """PROCEEDING_ANALYZER_SYSTEM output."""

    model_config = ConfigDict(extra="ignore", use_enum_values=True)

    is_court_document: bool = Field(
        description="True ONLY for documents issued by a court (not lawyer letters)."
    )
    court_level: ProceedingCourtLevel | None = Field(
        None, description="One of: ag, lg, olg, bgh."
    )
    court_name: str | None = Field(
        None, description="Actual court name (e.g. 'Amtsgericht Ingolstadt')."
    )
    az_court: str | None = Field(
        None, description="Single court file number (Aktenzeichen) in standard format."
    )
    subject_matter: str | None = None
    appeal_deadline_days: int | None = Field(
        None, description="Formal appeal deadline days if this is a ruling."
    )


class _Entity(BaseModel):
    model_config = ConfigDict(extra="ignore")

    type: _EntityType = Field(description="The entity category (lowercase).")
    name: str = Field(description="Canonical full official name.")
    context_quote: str | None = Field(
        None, description="10–30 words of surrounding text from the document."
    )


class EntityList(BaseModel):
    """ENTITY_EXTRACTOR_SYSTEM output."""

    model_config = ConfigDict(extra="ignore")

    entities: list[_Entity] = Field(default_factory=list)


class _NewClaim(BaseModel):
    model_config = ConfigDict(extra="ignore", use_enum_values=True)

    claim_text: str
    claim_type: ClaimType
    excerpt: str | None = None


class _EvidenceLink(BaseModel):
    model_config = ConfigDict(extra="ignore", use_enum_values=True)

    claim_id: int
    role: ClaimEvidenceRole
    excerpt: str | None = None


class ClaimExtraction(BaseModel):
    """CLAIM_EXTRACTOR_SYSTEM output."""

    model_config = ConfigDict(extra="ignore")

    new_claims: list[_NewClaim] = Field(default_factory=list)
    evidence_links: list[_EvidenceLink] = Field(default_factory=list)


class ClaimDedupJudgement(BaseModel):
    """CLAIM_DEDUP_JUDGE_SYSTEM output."""

    model_config = ConfigDict(extra="ignore")

    action: Literal["merge", "new"]
    confidence: _ConfidenceLevel
    rationale: str = ""


class _KeyPassage(BaseModel):
    model_config = ConfigDict(extra="ignore")

    text: str
    rationale: str | None = None


class _CostDelta(BaseModel):
    model_config = ConfigDict(extra="ignore")

    kind: _CostDeltaKind
    amount: float | None = None
    direction: _CostDirection = "none"
    description: str | None = None
    allocation: dict | None = None
    vat_included: bool | None = None
    offsets_signal_id: int | None = None


class _ManagementSummary(BaseModel):
    model_config = ConfigDict(extra="ignore")

    legal_significance: str | None = None
    required_action: str | None = None
    financial_impact: str | None = None


class _ActionItem(BaseModel):
    model_config = ConfigDict(extra="ignore")

    title: str
    action_type: _ActionType
    due_date: str | None = None
    description: str | None = None
    confidence: _ConfidenceLevel | None = None


class DocumentEnrichment(BaseModel):
    """DOCUMENT_ENRICHER_SYSTEM output."""

    model_config = ConfigDict(extra="ignore", use_enum_values=True)

    title: str | None = None
    issued_date: str | None = None
    significance_tier: SignificanceTier | None = None
    document_type: DocumentType | None = None
    key_passages: list[_KeyPassage] = Field(default_factory=list)
    cost_delta: _CostDelta | None = None
    management_summary: _ManagementSummary = Field(default_factory=_ManagementSummary)
    action_items: list[_ActionItem] = Field(default_factory=list)


class _Enclosure(BaseModel):
    model_config = ConfigDict(extra="ignore", use_enum_values=True)

    description: str
    attributed_originator: str | None = None
    originator_type: OriginatorType
    matched_filename: str | None = None


class _Bundle(BaseModel):
    model_config = ConfigDict(extra="ignore")

    cover_letter_doc_id: int | None = None
    enclosed: list[_Enclosure] = Field(default_factory=list)


class BatchAnalysis(BaseModel):
    """BATCH_ANALYZER_SYSTEM output."""

    model_config = ConfigDict(extra="ignore")

    bundles: list[_Bundle] = Field(default_factory=list)
    detected_actions: list[_ActionItem] = Field(default_factory=list)


class _Relationship(BaseModel):
    model_config = ConfigDict(extra="ignore")

    to_document_id: int
    relationship_type: _RelationshipType
    confidence: _ConfidenceLevel | None = None
    notes: str | None = None


class RelationshipDetection(BaseModel):
    """RELATIONSHIP_DETECTOR_SYSTEM output."""

    model_config = ConfigDict(extra="ignore")

    relationships: list[_Relationship] = Field(default_factory=list)


class CaseBrief(BaseModel):
    """CASE_BRIEF_SYSTEM output."""

    model_config = ConfigDict(extra="ignore")

    posture: str
    pressure_points: list[str] = Field(default_factory=list)
    next_move: str


class _Phase1Confidence(BaseModel):
    model_config = ConfigDict(extra="ignore")

    az_court: _ConfidenceLevel | None = None
    internal_id: _ConfidenceLevel | None = None
    case_title: _ConfidenceLevel | None = None
    sender: _ConfidenceLevel | None = None
    issued_date: _ConfidenceLevel | None = None
    originator: _ConfidenceLevel | None = None


class Phase1Metadata(BaseModel):
    """PHASE1_METADATA_SYSTEM output."""

    model_config = ConfigDict(extra="ignore", use_enum_values=True)

    az_court: str | None = Field(
        None,
        description="The official court Aktenzeichen / docket number (e.g. 12 F 100/24).",
    )
    internal_id: str | None = Field(
        None, description="The lawyer's internal reference number (e.g. 1234/25)."
    )
    case_title: str | None = Field(
        None,
        description="Short title: '[Party1] ./. [Party2] - [Matter]'. Surnames only.",
    )
    sender: str | None = Field(
        None, description="The organization or person who authored/sent the document."
    )
    issued_date: str | None = Field(
        None, description="The date shown on the document (ISO format: YYYY-MM-DD)."
    )
    originator: OriginatorType | None = Field(
        None, description="Categorize the document's author/source."
    )
    confidence: _Phase1Confidence = Field(default_factory=_Phase1Confidence)
    contradictions: list[str] = Field(
        default_factory=list,
        description="List of factual/procedural contradictions with case knowledge.",
    )
