from typing import Any

from pydantic import BaseModel, ConfigDict


class AISummarySchema(BaseModel):
    """Canonical 3-bullet management summary."""

    model_config = ConfigDict(from_attributes=True)

    legal_significance: str | None = None
    required_action: str | None = None
    financial_impact: str | None = None


class ExtractionConfidenceSchema(BaseModel):
    """Confidence levels for metadata extraction (high/medium/low)."""

    model_config = ConfigDict(from_attributes=True)

    sender: str = "low"
    date: str = "low"
    originator: str = "low"
    az_court: str = "low"
    internal_id: str = "low"


class CostCandidateSchema(BaseModel):
    """Extracted cost candidate from document content."""

    model_config = ConfigDict(from_attributes=True)

    type: str
    value: float
    raw_text: str | None = None
    context: str | None = None


class CostDeltaSchema(BaseModel):
    """Financial impact delta introduced by this document."""

    model_config = ConfigDict(from_attributes=True)

    amount: float
    direction: str  # incoming, outgoing, ruling, none
    description: str | None = ""


class KeyPassageSchema(BaseModel):
    """Important passage extracted from the document with rationale."""

    model_config = ConfigDict(from_attributes=True)

    text: str
    rationale: str | None = ""
    span: Any | None = None
    kind: str | None = None
    id: str | None = None
    start_offset: int | None = None
    end_offset: int | None = None
