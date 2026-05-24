from typing import Any, Literal

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
    issued_date: str = "low"
    az_court: str = "low"
    internal_id: str = "low"


class CostCandidateSchema(BaseModel):
    """Extracted cost candidate from document content."""

    model_config = ConfigDict(from_attributes=True)

    type: str
    value: float | str
    raw_text: str | None = None
    context: str | None = None


CostDeltaKind = Literal[
    "streitwert",  # Verfahrenswert / Streitwert — feeds RVG/GKG calculator
    "cost_ruling",  # Kostenentscheidung — who pays court + opposing counsel
    "invoice_lawyer",  # Lawyer Kostennote (incl. 19% VAT)
    "invoice_court",  # Court Gerichtskostenrechnung (no VAT)
    "vorschuss_lawyer",  # Lawyer advance / Prozesskostenhilfe Vorschuss
    "vorschuss_court",  # Court advance payment (Gerichtskostenvorschuss)
    "pkh_grant",  # Prozesskostenhilfe granted
    "pkh_denied",  # Prozesskostenhilfe denied
]


class CostDeltaSchema(BaseModel):
    """Typed cost signal extracted from a document by the AI enricher."""

    model_config = ConfigDict(from_attributes=True)

    kind: CostDeltaKind
    amount: float | None = None  # EUR; null for cost_ruling / pkh_*
    direction: str = "none"  # incoming | outgoing | ruling | none — UI tint
    description: str | None = ""
    # cost_ruling: {"own": 0.5, "opposing": 0.5} | {"each_own": True} | {"loser": 1.0}
    allocation: dict | None = None
    # invoice_*: True if amount already includes VAT, False if net, None if unknown
    vat_included: bool | None = None
    # invoice points to a prior vorschuss doc.id when the AI detects the link
    offsets_signal_id: int | None = None


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
