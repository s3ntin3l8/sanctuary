from datetime import datetime

from sqlalchemy import (
    JSON,
    Boolean,
    Column,
    DateTime,
    Float,
    ForeignKey,
    Index,
    Integer,
    String,
    Text,
)
from sqlalchemy import (
    Enum as SAEnum,
)
from sqlalchemy.orm import declarative_base, relationship

from app.models.enums import (
    ActionItemStatus,
    ActionItemType,
    CaseStatus,
    ClaimEvidenceRole,
    ClaimStatus,
    ClaimType,
    ConversationRole,
    ConversationScope,
    CostCategory,
    CostStatus,
    DocumentRole,
    DocumentType,
    EntityType,
    IngestBatchSourceType,
    IngestBatchStatus,
    IngestStatus,
    Jurisdiction,
    OriginatorType,
    ProceedingCourtLevel,
    ProceedingStatus,
    RelationshipConfidence,
    RelationshipType,
    SignificanceTier,
    UserReactionType,
)

Base = declarative_base()


class Document(Base):
    __tablename__ = "documents"
    __table_args__ = (
        Index("ix_documents_case_needs_review", "case_id", "needs_review"),
        Index("ix_documents_case_created", "case_id", "created_at"),
        Index("ix_documents_needs_review_created", "needs_review", "created_at"),
        Index("ix_documents_proceeding", "proceeding_id"),
        Index("ix_documents_ingest_batch", "ingest_batch_id"),
        Index("ix_documents_significance", "significance_tier"),
    )

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
    sender = Column(
        String, nullable=True, index=True
    )  # "Via: Email from [Sender] on [Date]"
    received_date = Column(
        DateTime, nullable=True
    )  # When the physical document was received
    created_at = Column(DateTime, default=datetime.now)
    needs_review = Column(Boolean, default=True, index=True)
    review_reasons = Column(
        JSON, default=list
    )  # e.g. ["missing_case_id", "missing_sender"]

    # Ingest pipeline status fields
    ingest_status = Column(
        SAEnum(IngestStatus), default=IngestStatus.PENDING, nullable=False
    )
    ingest_error = Column(Text, nullable=True)
    ingest_started_at = Column(DateTime, nullable=True)
    ingest_completed_at = Column(DateTime, nullable=True)

    # AI Management Summary fields
    ai_summary = Column(
        JSON, nullable=True
    )  # {"legal_significance": "...", "required_action": "...", "financial_impact": "..."}
    ai_summary_created_at = Column(DateTime, nullable=True)
    ai_summary_status = Column(
        String, default="pending", nullable=False
    )  # pending, generated, failed, stale, approved
    ai_summary_approved_at = Column(
        DateTime, nullable=True
    )  # timestamp when human approved

    # Extracted cost candidates (RVG, GKG, EUR amounts, Streitwert)
    cost_candidates = Column(
        JSON, nullable=True
    )  # [{"type": "rvg_position", "value": "...", ...}]

    # Extraction confidence scores (high/medium/low per field)
    extraction_confidence = Column(
        JSON, nullable=True
    )  # {"sender": "high", "date": "medium", "case_id": "high", "originator": "low"}

    # Structural metadata (page counts, headings, chunking info)
    meta = Column(JSON, nullable=True)

    # Self-referential relationship for 'Russian Doll' nesting
    parent_id = Column(Integer, ForeignKey("documents.id"), nullable=True, index=True)

    # Phase 1: bundle / proceeding grouping
    ingest_batch_id = Column(
        Integer, ForeignKey("ingest_batches.id"), nullable=True, index=True
    )
    proceeding_id = Column(
        Integer, ForeignKey("proceedings.id"), nullable=True, index=True
    )

    # Phase 1: structural and intelligence fields
    role = Column(SAEnum(DocumentRole), default=DocumentRole.STANDALONE, nullable=False)
    court_relay = Column(Boolean, default=False, nullable=False)
    attributed_originator = Column(String, nullable=True)  # true author, if routed
    document_type = Column(SAEnum(DocumentType), nullable=True)
    significance_tier = Column(SAEnum(SignificanceTier), nullable=True, index=True)
    thread_open = Column(Boolean, default=False, nullable=False)

    # Phase 1-B: AI-annotated reading & cost delta
    key_passages = Column(JSON, nullable=True)  # list of {text, rationale, span}
    cost_delta = Column(
        JSON, nullable=True
    )  # {amount, direction, description} single delta this doc introduces

    children = relationship(
        "Document", back_populates="parent", cascade="all, delete-orphan"
    )
    parent = relationship("Document", back_populates="children", remote_side=[id])
    ingest_batch = relationship("IngestBatch", back_populates="documents")
    proceeding = relationship("Proceeding", back_populates="documents")


class Case(Base):
    __tablename__ = "cases"

    id = Column(
        String, primary_key=True, index=True
    )  # Internal lead ID, e.g. ADV-992-K
    title = Column(String, nullable=False)
    status = Column(SAEnum(CaseStatus), default=CaseStatus.INTAKE, nullable=False)
    jurisdiction = Column(SAEnum(Jurisdiction), default=Jurisdiction.DE, nullable=False)
    created_at = Column(DateTime, default=datetime.now)
    closed_at = Column(DateTime, nullable=True)

    # Phase 1: cumulative AI intelligence + parties + exposure
    ai_brief = Column(JSON, nullable=True)  # living AI understanding of the case
    ai_brief_updated_at = Column(DateTime, nullable=True)
    parties = Column(JSON, nullable=True)  # known actors and their roles
    total_cost_exposure = Column(
        Integer, default=0, nullable=False
    )  # running total in cents

    proceedings = relationship(
        "Proceeding", back_populates="case", cascade="all, delete-orphan"
    )


class Proceeding(Base):
    """A court-level stage inside a case (AG, LG, OLG, BGH)."""

    __tablename__ = "proceedings"
    __table_args__ = (
        Index("ix_proceedings_case", "case_id"),
        Index("ix_proceedings_case_status", "case_id", "status"),
    )

    id = Column(Integer, primary_key=True, index=True)
    case_id = Column(String, ForeignKey("cases.id"), nullable=False, index=True)
    court_name = Column(String, nullable=False)  # "Amtsgericht Hamburg"
    court_level = Column(SAEnum(ProceedingCourtLevel), nullable=False)
    subject_matter = Column(String, nullable=True)  # "§ 1671 BGB, custody"
    az_court = Column(String, nullable=True)  # court file number e.g. "003 F 426/25"
    status = Column(
        SAEnum(ProceedingStatus), default=ProceedingStatus.ACTIVE, nullable=False
    )
    started_at = Column(DateTime, nullable=True)
    ended_at = Column(DateTime, nullable=True)
    created_at = Column(DateTime, default=datetime.now, nullable=False)

    case = relationship("Case", back_populates="proceedings")
    documents = relationship("Document", back_populates="proceeding")


class IngestBatch(Base):
    """A group of documents that arrived together (one email = one batch)."""

    __tablename__ = "ingest_batches"
    __table_args__ = (
        Index("ix_ingest_batches_case", "case_id"),
        Index("ix_ingest_batches_received", "received_at"),
    )

    id = Column(Integer, primary_key=True, index=True)
    source_type = Column(SAEnum(IngestBatchSourceType), nullable=False)
    received_at = Column(DateTime, default=datetime.now, nullable=False)
    sender_email = Column(String, nullable=True)
    subject = Column(String, nullable=True)
    raw_source_path = Column(String, nullable=True)  # path to original .eml/scan
    message_id = Column(String, index=True, nullable=True)
    case_id = Column(String, ForeignKey("cases.id"), nullable=True, index=True)
    proceeding_id = Column(
        Integer, ForeignKey("proceedings.id"), nullable=True, index=True
    )
    status = Column(
        SAEnum(IngestBatchStatus),
        default=IngestBatchStatus.PENDING,
        nullable=False,
    )
    created_at = Column(DateTime, default=datetime.now, nullable=False)
    analysis_queued_at = Column(DateTime, nullable=True)
    source_hash = Column(String, index=True, nullable=True)
    meta = Column(JSON, nullable=True)

    case = relationship("Case")
    proceeding = relationship("Proceeding")
    documents = relationship("Document", back_populates="ingest_batch")


class DocumentRelationship(Base):
    """Typed N:N edge between two documents (replaces the single in_reply_to FK idea)."""

    __tablename__ = "document_relationships"
    __table_args__ = (
        Index("ix_document_relationships_from", "from_document_id"),
        Index("ix_document_relationships_to", "to_document_id"),
        Index(
            "ix_document_relationships_type",
            "relationship_type",
        ),
    )

    id = Column(Integer, primary_key=True, index=True)
    from_document_id = Column(
        Integer, ForeignKey("documents.id"), nullable=False, index=True
    )
    to_document_id = Column(
        Integer, ForeignKey("documents.id"), nullable=False, index=True
    )
    relationship_type = Column(SAEnum(RelationshipType), nullable=False)
    confidence = Column(
        SAEnum(RelationshipConfidence),
        default=RelationshipConfidence.AI_DETECTED,
        nullable=False,
    )
    notes = Column(Text, nullable=True)
    created_at = Column(DateTime, default=datetime.now, nullable=False)

    from_document = relationship("Document", foreign_keys=[from_document_id])
    to_document = relationship("Document", foreign_keys=[to_document_id])


class ActionItem(Base):
    """Deadlines, court dates, and other case-level actions.

    Consolidates what used to be split across Deadline and Hearing tables.
    action_type distinguishes the kind of action.
    """

    __tablename__ = "action_items"
    __table_args__ = (
        Index("ix_action_items_case_due", "case_id", "due_date"),
        Index("ix_action_items_due_status", "due_date", "status"),
        Index("ix_action_items_proceeding", "proceeding_id"),
    )

    id = Column(Integer, primary_key=True, index=True)
    case_id = Column(String, ForeignKey("cases.id"), nullable=False, index=True)
    proceeding_id = Column(
        Integer, ForeignKey("proceedings.id"), nullable=True, index=True
    )
    source_document_id = Column(
        Integer, ForeignKey("documents.id"), nullable=True, index=True
    )

    title = Column(String, nullable=False)
    description = Column(Text, nullable=True)
    due_date = Column(DateTime, nullable=False, index=True)
    action_type = Column(
        SAEnum(ActionItemType),
        default=ActionItemType.DEADLINE,
        nullable=False,
    )
    status = Column(
        SAEnum(ActionItemStatus), default=ActionItemStatus.OPEN, nullable=False
    )
    location = Column(String, nullable=True)  # for court_date entries
    created_at = Column(DateTime, default=datetime.now, nullable=False)

    case = relationship("Case")
    proceeding = relationship("Proceeding")
    source_document = relationship("Document")


class Claim(Base):
    """An atomic factual or legal assertion made in a document (the Truth Map)."""

    __tablename__ = "claims"
    __table_args__ = (
        Index("ix_claims_case", "case_id"),
        Index("ix_claims_case_status", "case_id", "status"),
        Index("ix_claims_proceeding", "proceeding_id"),
    )

    id = Column(Integer, primary_key=True, index=True)
    case_id = Column(String, ForeignKey("cases.id"), nullable=False, index=True)
    proceeding_id = Column(
        Integer, ForeignKey("proceedings.id"), nullable=True, index=True
    )
    source_document_id = Column(
        Integer, ForeignKey("documents.id"), nullable=False, index=True
    )

    claim_text = Column(Text, nullable=False)
    claim_type = Column(SAEnum(ClaimType), default=ClaimType.FACTUAL, nullable=False)
    status = Column(SAEnum(ClaimStatus), default=ClaimStatus.ASSERTED, nullable=False)
    first_made_at = Column(DateTime, default=datetime.now, nullable=False)
    last_updated_at = Column(
        DateTime, default=datetime.now, onupdate=datetime.now, nullable=False
    )

    case = relationship("Case")
    proceeding = relationship("Proceeding")
    source_document = relationship("Document")
    evidence = relationship(
        "ClaimEvidence", back_populates="claim", cascade="all, delete-orphan"
    )


class ClaimEvidence(Base):
    """Link between a claim and a document that supports, contests, or refutes it."""

    __tablename__ = "claim_evidence"
    __table_args__ = (
        Index("ix_claim_evidence_claim", "claim_id"),
        Index("ix_claim_evidence_document", "document_id"),
    )

    id = Column(Integer, primary_key=True, index=True)
    claim_id = Column(Integer, ForeignKey("claims.id"), nullable=False, index=True)
    document_id = Column(
        Integer, ForeignKey("documents.id"), nullable=False, index=True
    )
    role = Column(SAEnum(ClaimEvidenceRole), nullable=False)
    excerpt = Column(Text, nullable=True)
    confidence = Column(
        SAEnum(RelationshipConfidence),
        default=RelationshipConfidence.AI_DETECTED,
        nullable=False,
    )
    created_at = Column(DateTime, default=datetime.now, nullable=False)

    claim = relationship("Claim", back_populates="evidence")
    document = relationship("Document")


class UserReaction(Base):
    """Strategic reaction the user tags on a document during triage.

    These become high-weight context for the AI when answering later
    questions about the document or case.
    """

    __tablename__ = "user_reactions"
    __table_args__ = (
        Index("ix_user_reactions_document", "document_id"),
        Index("ix_user_reactions_reaction", "reaction"),
    )

    id = Column(Integer, primary_key=True, index=True)
    document_id = Column(
        Integer, ForeignKey("documents.id"), nullable=False, index=True
    )
    user_id = Column(String, default="single_user", nullable=False)
    reaction = Column(SAEnum(UserReactionType), nullable=False)
    notes = Column(Text, nullable=True)
    created_at = Column(DateTime, default=datetime.now, nullable=False)

    document = relationship("Document")


class Conversation(Base):
    """AI chat thread scoped to either a case or a specific document."""

    __tablename__ = "conversations"
    __table_args__ = (Index("ix_conversations_scope", "scope_type", "scope_id"),)

    id = Column(Integer, primary_key=True, index=True)
    scope_type = Column(SAEnum(ConversationScope), nullable=False)
    scope_id = Column(
        String, nullable=False
    )  # case.id (str) or document.id (int-as-str)
    title = Column(String, nullable=True)
    created_at = Column(DateTime, default=datetime.now, nullable=False)

    messages = relationship(
        "ConversationMessage",
        back_populates="conversation",
        cascade="all, delete-orphan",
        order_by="ConversationMessage.created_at",
    )


class ConversationMessage(Base):
    __tablename__ = "conversation_messages"
    __table_args__ = (
        Index("ix_conversation_messages_conversation", "conversation_id"),
    )

    id = Column(Integer, primary_key=True, index=True)
    conversation_id = Column(
        Integer, ForeignKey("conversations.id"), nullable=False, index=True
    )
    role = Column(SAEnum(ConversationRole), nullable=False)
    content = Column(Text, nullable=False)
    context_document_ids = Column(JSON, nullable=True)  # [int, int, ...] sources
    created_at = Column(DateTime, default=datetime.now, nullable=False)

    conversation = relationship("Conversation", back_populates="messages")


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
                "action_items": True,
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
    __table_args__ = (
        Index("ix_legal_costs_case_status", "case_id", "status"),
        Index("ix_legal_costs_status_due", "status", "due_at"),
    )

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
    issued_at = Column(
        DateTime, nullable=True, index=True
    )  # Rechnung / Kostenfestsetzung
    due_at = Column(DateTime, nullable=True, index=True)  # Fälligkeitsdatum
    paid_at = Column(DateTime, nullable=True, index=True)  # Bezahlt am

    source_document_id = Column(Integer, ForeignKey("documents.id"), nullable=True)
    notes = Column(Text, nullable=True)
    created_at = Column(DateTime, default=datetime.now, nullable=False)

    case = relationship("Case")
    source_document = relationship("Document")


class Entity(Base):
    """
    Extracted entities from documents, aggregated per case.

    Enables quick pivot from case to key people, organizations,
    dates, financial amounts, and legal categories.
    """

    __tablename__ = "entities"
    __table_args__ = (Index("ix_entities_case_type", "case_id", "type"),)

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
