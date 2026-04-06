import enum
from sqlalchemy import (
    Column,
    Integer,
    String,
    Float,
    ForeignKey,
    DateTime,
    Text,
    Boolean,
    Enum as SAEnum,
    JSON,
)
from sqlalchemy.orm import declarative_base, relationship
from datetime import datetime


class CaseStatus(str, enum.Enum):
    INTAKE = "intake"
    DISCOVERY = "discovery"
    PRE_TRIAL = "pre_trial"
    TRIAL = "trial"
    POST_TRIAL = "post_trial"
    CLOSED = "closed"


class OriginatorType(str, enum.Enum):
    """Maps to the border-l-4 originator stripes from GEMINI.md §4."""

    COURT = "court"  # Blue #0369A1 — Gavel icon
    OPPOSING = "opposing"  # Red  #B91C1C — Warning icon
    OWN = "own"  # Green #047857 — Shield icon
    UNKNOWN = "unknown"  # Neutral — for unclassified docs


Base = declarative_base()


class Document(Base):
    __tablename__ = "documents"

    id = Column(Integer, primary_key=True, index=True)
    title = Column(String, nullable=False, index=True)
    content = Column(Text, nullable=True)
    content_embedding = Column(
        Text, nullable=True
    )  # sqlite-vec f32 binary blob (future semantic search)
    case_id = Column(String, nullable=True, index=True)
    file_path = Column(String, nullable=True)
    content_hash = Column(String(64), nullable=True, index=True)  # SHA-256 hex digest
    originator_type = Column(
        SAEnum(OriginatorType), default=OriginatorType.UNKNOWN, nullable=False
    )
    sender = Column(String, nullable=True)  # "Via: Email from [Sender] on [Date]"
    received_date = Column(
        DateTime, nullable=True
    )  # When the physical document was received
    created_at = Column(DateTime, default=datetime.now)
    needs_review = Column(Boolean, default=True, index=True)
    review_reasons = Column(
        JSON, default=list
    )  # e.g. ["missing_case_id", "missing_sender"]

    # AI Management Summary fields
    ai_summary = Column(
        JSON, nullable=True
    )  # {"legal_significance": "...", "required_action": "...", "financial_impact": "..."}
    ai_summary_created_at = Column(DateTime, nullable=True)
    ai_summary_status = Column(
        String, default="pending", nullable=False
    )  # pending, generated, failed, stale

    # Extracted cost candidates (RVG, GKG, EUR amounts, Streitwert)
    cost_candidates = Column(
        JSON, nullable=True
    )  # [{"type": "rvg_position", "value": "...", ...}]

    # Self-referential relationship for 'Russian Doll' nesting
    parent_id = Column(Integer, ForeignKey("documents.id"), nullable=True)

    children = relationship(
        "Document", back_populates="parent", cascade="all, delete-orphan"
    )
    parent = relationship("Document", back_populates="children", remote_side=[id])


class Case(Base):
    __tablename__ = "cases"

    id = Column(String, primary_key=True, index=True)  # Internal ID e.g. ADV-992-K
    title = Column(String, nullable=False)
    court_id = Column(String, nullable=True)  # Official docket ID
    status = Column(SAEnum(CaseStatus), default=CaseStatus.INTAKE, nullable=False)
    created_at = Column(DateTime, default=datetime.now)
    closed_at = Column(DateTime, nullable=True)


class UserSettings(Base):
    __tablename__ = "user_settings"

    id = Column(Integer, primary_key=True)
    user_id = Column(String, default="single_user", unique=True, nullable=False)
    settings_json = Column(
        JSON,
        default=lambda: {
            "theme": "dark",
            "sidebar_collapsed": False,
            "default_view": "dashboard",
            "dashboard_cards": {
                "deadlines": True,
                "hearings": True,
                "costs": True,
                "documents": True,
            },
        },
    )
    updated_at = Column(DateTime, default=datetime.now, onupdate=datetime.now)


class SavedSearch(Base):
    __tablename__ = "saved_searches"

    id = Column(Integer, primary_key=True)
    user_id = Column(String, default="single_user", nullable=False)
    name = Column(String, nullable=False)
    filter_json = Column(JSON, nullable=False)
    created_at = Column(DateTime, default=datetime.now)


class Deadline(Base):
    __tablename__ = "deadlines"

    id = Column(Integer, primary_key=True, index=True)
    case_id = Column(String, ForeignKey("cases.id"), nullable=False, index=True)
    title = Column(String, nullable=False)
    description = Column(Text, nullable=True)
    due_at = Column(DateTime, nullable=False, index=True)
    completed = Column(Boolean, default=False, nullable=False, index=True)
    source_document_id = Column(
        Integer, ForeignKey("documents.id"), nullable=True, index=True
    )
    created_at = Column(DateTime, default=datetime.now, nullable=False)

    case = relationship("Case")
    source_document = relationship("Document")


class Hearing(Base):
    __tablename__ = "hearings"

    id = Column(Integer, primary_key=True, index=True)
    case_id = Column(String, ForeignKey("cases.id"), nullable=False, index=True)
    title = Column(String, nullable=False)
    description = Column(Text, nullable=True)
    scheduled_for = Column(DateTime, nullable=False, index=True)
    location = Column(String, nullable=True)
    source_document_id = Column(
        Integer, ForeignKey("documents.id"), nullable=True, index=True
    )
    created_at = Column(DateTime, default=datetime.now, nullable=False)

    case = relationship("Case")
    source_document = relationship("Document")


class CostCategory(str, enum.Enum):
    """German legal cost categories (Kostenkategorien)."""

    GERICHTSKOSTEN = "gerichtskosten"  # Court fees — GKG
    ANWALTSKOSTEN = "anwaltskosten"  # Own lawyer fees — RVG
    ANWALTSKOSTEN_GEGNER = (
        "anwaltskosten_gegner"  # Opposing counsel fees (§91 ZPO claim/liability)
    )
    SACHVERSTAENDIGER = "sachverstaendiger"  # Expert witnesses — JVEG
    VORSCHUSS = "vorschuss"  # Advance payments (Gerichtskostenvorschuss)
    VOLLSTRECKUNG = "vollstreckung"  # Enforcement costs
    AUSLAGEN = "auslagen"  # Out-of-pocket expenses (RVG Nr. 7000 ff.)
    SONSTIGES = "sonstiges"  # Other


class CostStatus(str, enum.Enum):
    """Payment/reimbursement status of a cost position."""

    OFFEN = "offen"  # Due but unpaid (ausstehend)
    BEZAHLT = "bezahlt"  # Paid by us
    ERSTATTET = "erstattet"  # Reimbursed by opposing party (§91 ZPO)
    TEILWEISE = "teilweise"  # Partially paid / partially reimbursed
    STRITTIG = "strittig"  # Disputed


class LegalCost(Base):
    """
    A single cost position in the German legal cost system.

    German legal costs are governed by:
    - RVG (Rechtsanwaltsvergütungsgesetz) — lawyer fees
    - GKG (Gerichtskostengesetz) — court fees
    - JVEG (Justizvergütungs- und -entschädigungsgesetz) — expert/witness fees
    - §§ 91–107 ZPO — cost allocation ("loser pays")

    The Streitwert (value in dispute) drives all RVG and GKG calculations.
    """

    __tablename__ = "legal_costs"

    id = Column(Integer, primary_key=True, index=True)
    case_id = Column(String, ForeignKey("cases.id"), nullable=False, index=True)

    category = Column(SAEnum(CostCategory), nullable=False)
    status = Column(
        SAEnum(CostStatus), default=CostStatus.OFFEN, nullable=False, index=True
    )

    # Human-readable label, e.g. "Verfahrensgebühr 1. Instanz"
    title = Column(String, nullable=False)
    # Statutory position reference, e.g. "Nr. 3100 VV RVG" or "KV GKG Nr. 1210"
    rvg_position = Column(String, nullable=True)

    # Amounts in EUR
    amount_net = Column(Float, nullable=False)  # Nettobetrag
    vat_rate = Column(Float, default=0.0)  # 0.19 for lawyer, 0.0 for court
    amount_gross = Column(Float, nullable=False)  # Bruttobetrag (net + VAT)
    amount_paid = Column(Float, default=0.0)  # Bereits bezahlt von uns
    amount_reimbursed = Column(Float, default=0.0)  # Vom Gegner erstattet

    # German-specific metadata
    streitwert = Column(Float, nullable=True)  # Streitwert basis for this position
    gebuehren_faktor = Column(
        Float, nullable=True
    )  # RVG factor, e.g. 1.3 for Verfahrensgebühr
    is_reimbursable = Column(Boolean, default=True)  # Erstattungsfähig nach §91 ZPO

    # Dates
    issued_at = Column(DateTime, nullable=True)  # Rechnung / Kostenfestsetzung
    due_at = Column(DateTime, nullable=True)  # Fälligkeitsdatum
    paid_at = Column(DateTime, nullable=True)  # Bezahlt am

    source_document_id = Column(Integer, ForeignKey("documents.id"), nullable=True)
    notes = Column(Text, nullable=True)
    created_at = Column(DateTime, default=datetime.now, nullable=False)

    case = relationship("Case")
    source_document = relationship("Document")


class EntityType(str, enum.Enum):
    """Types of entities extracted from documents."""

    PERSON = "person"
    ORGANIZATION = "organization"
    DATE = "date"
    FINANCIAL = "financial"
    LEGAL_CATEGORY = "legal_category"


class Entity(Base):
    """
    Extracted entities from documents, aggregated per case.

    Enables quick pivot from case to key people, organizations,
    dates, financial amounts, and legal categories.
    """

    __tablename__ = "entities"

    id = Column(Integer, primary_key=True, index=True)
    case_id = Column(String, ForeignKey("cases.id"), nullable=False, index=True)

    type = Column(SAEnum(EntityType), nullable=False, index=True)
    name = Column(String, nullable=False, index=True)

    # Source tracking
    source_document_id = Column(Integer, ForeignKey("documents.id"), nullable=True)

    # Additional metadata (confidence, positions, extracted context)
    extra_data = Column(JSON, nullable=True)

    created_at = Column(DateTime, default=datetime.now, nullable=False)

    case = relationship("Case")
    source_document = relationship("Document")
