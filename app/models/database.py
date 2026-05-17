import shutil
from datetime import UTC, datetime
from pathlib import Path

from sqlalchemy import event, inspect


def _utcnow():
    return datetime.now(UTC)


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
    UniqueConstraint,
)
from sqlalchemy import (
    Enum as SAEnum,
)
from sqlalchemy import (
    text as _sa_text,
)
from sqlalchemy.orm import declarative_base, relationship, validates

from app.core.validators import normalize_case_id
from app.models.enums import (
    ActionItemStatus,
    ActionItemType,
    AuditEventType,
    CaseStatus,
    CaseType,
    ClaimEvidenceRole,
    ClaimStatus,
    ClaimType,
    CostCategory,
    CostStatus,
    DocumentRole,
    DocumentStatus,
    DocumentType,
    EntityType,
    IngestBatchSourceType,
    IngestBatchStatus,
    Jurisdiction,
    OriginatorType,
    PipelineStage,
    PipelineState,
    ProceedingCourtLevel,
    ProceedingStatus,
    ProposalConfidence,
    ProposalStatus,
    RelationshipConfidence,
    RelationshipType,
    SignificanceTier,
    StageStatus,
    UserReactionType,
)

Base = declarative_base()


class Document(Base):
    __tablename__ = "documents"
    __table_args__ = (
        Index("ix_documents_case_needs_review", "case_id", "needs_review"),
        Index("ix_documents_case_created", "case_id", "ingest_date"),
        Index("ix_documents_needs_review_created", "needs_review", "ingest_date"),
        Index("ix_documents_proceeding", "proceeding_id"),
        Index("ix_documents_ingest_batch", "ingest_batch_id"),
        Index("ix_documents_significance", "significance_tier"),
        Index("ix_documents_pipeline_state", "pipeline_state"),
        Index("ix_documents_sub_group", "sub_group_id"),
    )

    id = Column(Integer, primary_key=True, index=True)
    title = Column(String, nullable=False, index=True)
    content = Column(Text, nullable=True)
    case_id = Column(
        String,
        ForeignKey("cases.id", ondelete="SET NULL"),
        nullable=True,
        index=True,
    )
    file_path = Column(String, nullable=True)
    original_filename = Column(String, nullable=True)
    content_hash = Column(String(64), nullable=True, index=True)  # SHA-256 hex digest
    originator_type = Column(
        SAEnum(OriginatorType), default=OriginatorType.UNKNOWN, nullable=False
    )
    internal_id = Column(
        String, nullable=True, index=True
    )  # lawyer's file ref, e.g. "8124-25"
    az_court = Column(
        String, nullable=True, index=True
    )  # AI-extracted court Aktenzeichen, e.g. "003 F 426/25" — kept on the
    # doc as a fallback hint for the metadata review HUD when no Proceeding
    # has been linked yet. The Proceeding row is the authoritative source
    # once one exists.
    sender = Column(
        String, nullable=True, index=True
    )  # "Via: Email from [Sender] on [Date]"
    received_date = Column(
        DateTime, nullable=True
    )  # When the physical document was received
    issued_date = Column(
        DateTime, nullable=True, index=True
    )  # Date on the document itself (Datum:, Date: header, Bescheiddatum)
    ingest_date = Column(DateTime, default=_utcnow)
    needs_review = Column(Boolean, default=True, index=True)
    status = Column(
        SAEnum(DocumentStatus),
        default=DocumentStatus.ACTIVE,
        nullable=False,
        index=True,
    )
    review_reasons = Column(
        JSON, default=list
    )  # e.g. ["missing_case_id", "missing_sender"]

    # Pipeline tracking
    pipeline_state = Column(
        SAEnum(PipelineState, values_callable=lambda obj: [e.value for e in obj]),
        default=PipelineState.PENDING,
        nullable=False,
    )
    pipeline_stages = Column(
        JSON, default=dict
    )  # per-stage records keyed by stage name

    # AI Management Summary fields
    ai_summary = Column(
        JSON, nullable=True
    )  # {"legal_significance": "...", "required_action": "...", "financial_impact": "..."}
    ai_summary_created_at = Column(DateTime, nullable=True)
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

    page_count = Column(Integer, nullable=False, default=0)

    # Self-referential relationship for 'Russian Doll' nesting
    parent_id = Column(
        Integer,
        ForeignKey("documents.id", ondelete="SET NULL"),
        nullable=True,
        index=True,
    )
    sub_group_id = Column(
        Integer,
        ForeignKey("batch_sub_groups.id", ondelete="SET NULL"),
        nullable=True,
        index=True,
    )
    sub_group_sort_order = Column(Integer, nullable=True, default=0)

    # Phase 1: bundle / proceeding grouping
    ingest_batch_id = Column(
        Integer,
        ForeignKey("ingest_batches.id", ondelete="SET NULL"),
        nullable=True,
        index=True,
    )
    proceeding_id = Column(
        Integer,
        ForeignKey("proceedings.id", ondelete="SET NULL"),
        nullable=True,
        index=True,
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
    sub_group = relationship("BatchSubGroup", back_populates="documents")
    ingest_batch = relationship("IngestBatch", back_populates="documents")
    proceeding = relationship("Proceeding", back_populates="documents")
    pins = relationship(
        "DocumentPin", back_populates="document", cascade="all, delete-orphan"
    )
    reactions = relationship(
        "UserReaction", back_populates="document", cascade="all, delete-orphan"
    )
    claim_evidence = relationship(
        "ClaimEvidence", back_populates="document", cascade="all, delete-orphan"
    )
    stage_rows = relationship(
        "DocumentPipelineStage",
        back_populates="document",
        cascade="all, delete-orphan",
        lazy="dynamic",
    )

    @validates("case_id")
    def validate_case_id(self, key, case_id):
        return normalize_case_id(case_id)


class DocumentPipelineStage(Base):
    __tablename__ = "document_pipeline_stages"

    document_id = Column(
        Integer,
        ForeignKey("documents.id", ondelete="CASCADE"),
        primary_key=True,
        nullable=False,
    )
    stage = Column(
        SAEnum(PipelineStage, values_callable=lambda obj: [e.value for e in obj]),
        primary_key=True,
        nullable=False,
    )
    status = Column(
        SAEnum(StageStatus, values_callable=lambda obj: [e.value for e in obj]),
        nullable=False,
    )
    started_at = Column(DateTime, nullable=True)
    completed_at = Column(DateTime, nullable=True)
    error = Column(Text, nullable=True)
    reason = Column(String, nullable=True)
    attempt = Column(Integer, nullable=True)
    max_attempts = Column(Integer, nullable=True)
    next_at = Column(DateTime, nullable=True)

    document = relationship("Document", back_populates="stage_rows")

    __table_args__ = (
        Index("ix_dps_status", "status"),
        Index("ix_dps_stage_status", "stage", "status"),
    )


def generate_normalized_filename(target) -> str:
    """Generate a normalized filename: YYYYMMDD_sanitized-title.ext"""
    import re

    # 1. Date (YYYYMMDD)
    date_val = target.issued_date or target.ingest_date or datetime.now(UTC)
    date_str = date_val.strftime("%Y%m%d")

    # 2. Sanitized Title
    title = target.title or "unnamed"
    # Replace non-alphanumeric (allowing umlauts) with underscores
    safe_title = re.sub(r"[^a-zA-Z0-9\u00C0-\u017F]+", "_", title).strip("_")

    # 3. Extension
    ext = ""
    if target.file_path:
        ext = Path(target.file_path).suffix
    if not ext and target.original_filename:
        ext = Path(target.original_filename).suffix

    if not ext:
        ext = ".pdf"

    return f"{date_str}_{safe_title}{ext}"


@event.listens_for(Document, "before_update")
def move_document_file_on_assignment(mapper, connection, target):
    """Physically move the document file when assigned to a case/proceeding.

    Only triggers when needs_review is False (finalized).
    """
    from app.config import DATA_DIR

    if target.needs_review or not target.file_path:
        return

    state = inspect(target)
    h_case = state.attrs.case_id.history
    h_proc = state.attrs.proceeding_id.history
    h_review = state.attrs.needs_review.history

    # Move if it just left triage, or if it was already out and case/proc changed.
    moved_to_final = h_review.has_changes() and not target.needs_review
    assignment_changed = h_case.has_changes() or h_proc.has_changes()

    if not (moved_to_final or assignment_changed):
        return

    if not target.case_id or target.case_id == "_TRIAGE":
        return

    old_path = Path(target.file_path)
    if not old_path.is_absolute():
        old_path = DATA_DIR / old_path

    if not old_path.exists():
        return

    # Calculate new directory: data/CASE_ID/AZ_COURT/
    new_dir = DATA_DIR / target.case_id
    if target.proceeding_id:
        # Fetch az_court for folder name. We use connection to avoid session issues.
        res = connection.execute(
            _sa_text("SELECT az_court FROM proceedings WHERE id = :id"),
            {"id": target.proceeding_id},
        ).fetchone()
        az_court = res[0] if res else None

        if az_court:
            # Sanitize: spaces -> _, / -> -
            safe_az = az_court.replace(" ", "_").replace("/", "-")
            new_dir = new_dir / safe_az
        else:
            new_dir = new_dir / f"proc_{target.proceeding_id}"

    new_dir.mkdir(parents=True, exist_ok=True)
    normalized_name = generate_normalized_filename(target)
    new_path = new_dir / normalized_name

    # Collision handling
    if new_path.exists() and new_path.resolve() != old_path.resolve():
        stem = new_path.stem
        suffix = new_path.suffix
        counter = 1
        while new_path.exists():
            new_path = new_dir / f"{stem}_{counter}{suffix}"
            counter += 1

    if new_path.resolve() != old_path.resolve():
        shutil.move(str(old_path), str(new_path))
        target.file_path = str(new_path.relative_to(DATA_DIR))


class Case(Base):
    __tablename__ = "cases"

    id = Column(
        String, primary_key=True, index=True
    )  # Internal lead ID, e.g. ADV-992-K
    title = Column(String, nullable=False)
    status = Column(SAEnum(CaseStatus), default=CaseStatus.INTAKE, nullable=False)
    jurisdiction = Column(SAEnum(Jurisdiction), default=Jurisdiction.DE, nullable=False)
    ingest_date = Column(DateTime, default=_utcnow)
    closed_at = Column(DateTime, nullable=True)
    is_draft = Column(Boolean, default=False, nullable=False)

    case_type = Column(SAEnum(CaseType), default=CaseType.CIVIL, nullable=False)
    # Pre-ruling: assume we lose and must pay opposing counsel (worst case).
    # Family cases default to False (§81 FamFG Kostenteilung is the norm).
    assume_worst_case = Column(Boolean, default=True, nullable=False)

    # Phase 1: cumulative AI intelligence + parties + exposure
    ai_brief = Column(JSON, nullable=True)  # living AI understanding of the case
    ai_brief_updated_at = Column(DateTime, nullable=True)
    parties = Column(JSON, nullable=True)  # known actors and their roles
    opposing_parties = Column(JSON, nullable=True)  # per-case opposing party names
    total_cost_exposure = Column(
        Integer, default=0, nullable=False
    )  # running total in cents

    proceedings = relationship(
        "Proceeding", back_populates="case", cascade="all, delete-orphan"
    )

    @validates("id")
    def validate_id(self, key, case_id):
        return normalize_case_id(case_id)


class Proceeding(Base):
    """A court-level stage inside a case (AG, LG, OLG, BGH)."""

    __tablename__ = "proceedings"
    __table_args__ = (
        Index("ix_proceedings_case", "case_id"),
        Index("ix_proceedings_case_status", "case_id", "status"),
    )

    id = Column(Integer, primary_key=True, index=True)
    case_id = Column(
        String,
        ForeignKey("cases.id", ondelete="CASCADE"),
        nullable=False,
        index=True,
    )
    court_name = Column(String, nullable=False)  # "Amtsgericht Hamburg"
    court_level = Column(SAEnum(ProceedingCourtLevel), nullable=False)
    subject_matter = Column(String, nullable=True)  # "§ 1671 BGB, custody"
    az_court = Column(String, nullable=True)  # court file number e.g. "003 F 426/25"
    status = Column(
        SAEnum(ProceedingStatus), default=ProceedingStatus.ACTIVE, nullable=False
    )
    started_at = Column(DateTime, nullable=True)
    ended_at = Column(DateTime, nullable=True)
    ingest_date = Column(DateTime, default=_utcnow, nullable=False)
    # AI-suggested proceedings start as drafts; flipped to False on user
    # confirmation in /triage/confirm. Mirrors Case.is_draft.
    is_draft = Column(Boolean, default=False, nullable=False)

    case = relationship("Case", back_populates="proceedings")
    documents = relationship("Document", back_populates="proceeding")

    @validates("case_id")
    def validate_case_id(self, key, case_id):
        return normalize_case_id(case_id)


class IngestBatch(Base):
    """A group of documents that arrived together (one email = one batch)."""

    __tablename__ = "ingest_batches"
    __table_args__ = (
        Index("ix_ingest_batches_case", "case_id"),
        Index("ix_ingest_batches_received", "received_at"),
    )

    id = Column(Integer, primary_key=True, index=True)
    source_type = Column(SAEnum(IngestBatchSourceType), nullable=False)
    received_at = Column(DateTime, default=_utcnow, nullable=False)
    sender_email = Column(String, nullable=True)
    subject = Column(String, nullable=True)
    raw_source_path = Column(String, nullable=True)  # path to original .eml/scan
    message_id = Column(String, index=True, nullable=True)
    case_id = Column(
        String,
        ForeignKey("cases.id", ondelete="SET NULL"),
        nullable=True,
        index=True,
    )
    proceeding_id = Column(
        Integer,
        ForeignKey("proceedings.id", ondelete="SET NULL"),
        nullable=True,
        index=True,
    )
    status = Column(
        SAEnum(IngestBatchStatus),
        default=IngestBatchStatus.PENDING,
        nullable=False,
    )
    ingest_date = Column(DateTime, default=_utcnow, nullable=False)
    analysis_queued_at = Column(DateTime, nullable=True)
    source_hash = Column(String, index=True, nullable=True)
    meta = Column(JSON, nullable=True)
    detected_actions = Column(JSON, nullable=True)

    case = relationship("Case")
    proceeding = relationship("Proceeding")
    documents = relationship("Document", back_populates="ingest_batch")
    sub_groups = relationship(
        "BatchSubGroup",
        back_populates="batch",
        order_by="BatchSubGroup.sort_order",
        cascade="all, delete-orphan",
    )

    @validates("case_id")
    def validate_case_id(self, key, case_id):
        return normalize_case_id(case_id)

    @property
    def key(self) -> str:
        return f"batch-{self.id}"

    @property
    def batch_id(self) -> int:
        return self.id

    @property
    def pipeline_summary(self) -> dict:
        from collections import Counter

        counts = Counter(
            (d.pipeline_state.value if d.pipeline_state else "pending")
            for d in self.documents
        )
        return {"total": len(self.documents), **counts}


class BatchSubGroup(Base):
    """A manually-created grouping of documents within an IngestBatch.

    When BatchSubGroup rows exist for a batch, the triage tree picker uses them
    for grouping instead of the auto parent_groups hierarchy.
    """

    __tablename__ = "batch_sub_groups"
    __table_args__ = (Index("ix_batch_sub_groups_batch", "batch_id"),)

    id = Column(Integer, primary_key=True, index=True)
    batch_id = Column(
        Integer,
        ForeignKey("ingest_batches.id", ondelete="CASCADE"),
        nullable=False,
        index=True,
    )
    label = Column(String, nullable=True)  # None = auto-derived from lead doc title
    sort_order = Column(Integer, nullable=False, default=0)
    created_at = Column(DateTime, default=_utcnow, nullable=False)

    batch = relationship("IngestBatch", back_populates="sub_groups")
    documents = relationship("Document", back_populates="sub_group")


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
        UniqueConstraint(
            "from_document_id",
            "to_document_id",
            "relationship_type",
            name="uq_document_relationships_edge",
        ),
    )

    id = Column(Integer, primary_key=True, index=True)
    from_document_id = Column(
        Integer,
        ForeignKey("documents.id", ondelete="CASCADE"),
        nullable=False,
        index=True,
    )
    to_document_id = Column(
        Integer,
        ForeignKey("documents.id", ondelete="CASCADE"),
        nullable=False,
        index=True,
    )
    relationship_type = Column(SAEnum(RelationshipType), nullable=False)
    confidence = Column(
        SAEnum(RelationshipConfidence),
        default=RelationshipConfidence.AI_DETECTED,
        nullable=False,
    )
    notes = Column(Text, nullable=True)
    ingest_date = Column(DateTime, default=_utcnow, nullable=False)

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
        UniqueConstraint(
            "case_id", "due_date", "action_type", name="uq_action_items_case_due_type"
        ),
    )

    id = Column(Integer, primary_key=True, index=True)
    case_id = Column(
        String,
        ForeignKey("cases.id", ondelete="CASCADE"),
        nullable=False,
        index=True,
    )
    proceeding_id = Column(
        Integer,
        ForeignKey("proceedings.id", ondelete="SET NULL"),
        nullable=True,
        index=True,
    )
    source_document_id = Column(
        Integer,
        ForeignKey("documents.id", ondelete="SET NULL"),
        nullable=True,
        index=True,
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
    ingest_date = Column(DateTime, default=_utcnow, nullable=False)

    case = relationship("Case")
    proceeding = relationship("Proceeding")
    source_document = relationship("Document")

    @validates("case_id")
    def validate_case_id(self, key, case_id):
        return normalize_case_id(case_id)


class Claim(Base):
    """An atomic factual, legal, or procedural proposition (the Truth Map node).

    Wave 2A: claims are global. Case context lives entirely on `ClaimEvidence`
    — every claim has at least one `ClaimEvidence(role=ASSERTS)` row whose
    document carries the case_id, and queries that need case scope join
    through ClaimEvidence → Document → Document.case_id. A finding established
    in case A's documents can be evidence-linked from case B's documents
    without duplicating the claim record.
    """

    __tablename__ = "claims"
    __table_args__ = (Index("ix_claims_status", "status"),)

    id = Column(Integer, primary_key=True, index=True)

    claim_text = Column(Text, nullable=False)
    claim_type = Column(SAEnum(ClaimType), default=ClaimType.FACTUAL, nullable=False)
    status = Column(SAEnum(ClaimStatus), default=ClaimStatus.ASSERTED, nullable=False)
    is_precedent = Column(Boolean, default=False, nullable=False)
    first_made_at = Column(DateTime, default=_utcnow, nullable=False)
    last_updated_at = Column(
        DateTime, default=_utcnow, onupdate=_utcnow, nullable=False
    )
    dismissed_at = Column(DateTime, nullable=True, index=True)

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
    claim_id = Column(
        Integer,
        ForeignKey("claims.id", ondelete="CASCADE"),
        nullable=False,
        index=True,
    )
    document_id = Column(
        Integer,
        ForeignKey("documents.id", ondelete="CASCADE"),
        nullable=False,
        index=True,
    )
    role = Column(SAEnum(ClaimEvidenceRole), nullable=False)
    excerpt = Column(Text, nullable=True)
    confidence = Column(
        SAEnum(RelationshipConfidence),
        default=RelationshipConfidence.AI_DETECTED,
        nullable=False,
    )
    ingest_date = Column(DateTime, default=_utcnow, nullable=False)

    claim = relationship("Claim", back_populates="evidence")
    document = relationship("Document", back_populates="claim_evidence")


class ClaimMergeProposal(Base):
    """AI-proposed merge between a freshly-extracted claim and an existing one.

    Wave 2B. The dedup judge sees a new claim coming out of the extractor
    against the top-K embedding-nearest existing claims and emits a "merge"
    verdict for high-confidence matches. We don't auto-apply: a row lands
    here as PENDING. User confirms → new_claim's evidence rows get folded
    onto existing_claim_id and new_claim is deleted. User dismisses → both
    claims stand independently.
    """

    __tablename__ = "claim_merge_proposals"
    __table_args__ = (
        Index(
            "ix_claim_merge_proposals_status_pending",
            "status",
            sqlite_where=_sa_text("status = 'PENDING'"),
        ),
        Index("ix_claim_merge_proposals_new_claim", "new_claim_id"),
        Index("ix_claim_merge_proposals_existing_claim", "existing_claim_id"),
    )

    id = Column(Integer, primary_key=True, index=True)
    new_claim_id = Column(
        Integer,
        ForeignKey("claims.id", ondelete="CASCADE"),
        nullable=False,
    )
    existing_claim_id = Column(
        Integer,
        ForeignKey("claims.id", ondelete="CASCADE"),
        nullable=False,
    )
    confidence = Column(SAEnum(ProposalConfidence), nullable=False)
    rationale = Column(Text, nullable=True)
    status = Column(
        SAEnum(ProposalStatus), default=ProposalStatus.PENDING, nullable=False
    )
    proposed_at = Column(DateTime, default=_utcnow, nullable=False)
    resolved_at = Column(DateTime, nullable=True)

    new_claim = relationship("Claim", foreign_keys=[new_claim_id])
    existing_claim = relationship("Claim", foreign_keys=[existing_claim_id])


class ClaimEvidenceProposal(Base):
    """AI-proposed evidence link from a document onto an existing claim.

    Wave 2B. Replaces the old auto-apply path in the claim extractor where
    the AI's `evidence_links` immediately wrote ClaimEvidence rows and
    sometimes auto-flipped status to REFUTED — frequently wrongly. Now the
    AI proposes; the user confirms before any evidence row lands.
    """

    __tablename__ = "claim_evidence_proposals"
    __table_args__ = (
        Index(
            "ix_claim_evidence_proposals_status_pending",
            "status",
            sqlite_where=_sa_text("status = 'PENDING'"),
        ),
        Index("ix_claim_evidence_proposals_target_claim", "target_claim_id"),
        Index(
            "ix_claim_evidence_proposals_source_document",
            "source_document_id",
        ),
    )

    id = Column(Integer, primary_key=True, index=True)
    target_claim_id = Column(
        Integer,
        ForeignKey("claims.id", ondelete="CASCADE"),
        nullable=False,
    )
    source_document_id = Column(
        Integer,
        ForeignKey("documents.id", ondelete="CASCADE"),
        nullable=False,
    )
    proposed_role = Column(SAEnum(ClaimEvidenceRole), nullable=False)
    excerpt = Column(Text, nullable=True)
    rationale = Column(Text, nullable=True)
    confidence = Column(SAEnum(ProposalConfidence), nullable=False)
    status = Column(
        SAEnum(ProposalStatus), default=ProposalStatus.PENDING, nullable=False
    )
    proposed_at = Column(DateTime, default=_utcnow, nullable=False)
    resolved_at = Column(DateTime, nullable=True)

    target_claim = relationship("Claim", foreign_keys=[target_claim_id])
    source_document = relationship("Document", foreign_keys=[source_document_id])


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
    ingest_date = Column(DateTime, default=_utcnow, nullable=False)

    document = relationship("Document", back_populates="reactions")


class DocumentPin(Base):
    """Passage-anchored margin pin — close-reading annotation dropped during HUD review.

    Distinct from UserReaction (case-wide strategic tag). Pins are positional
    and may carry long-form notes.
    """

    __tablename__ = "document_pins"
    __table_args__ = (
        Index("ix_document_pins_document", "document_id"),
        Index("ix_document_pins_passage", "passage_id"),
    )

    id = Column(Integer, primary_key=True, index=True)
    document_id = Column(
        Integer, ForeignKey("documents.id"), nullable=False, index=True
    )
    passage_id = Column(String(12), nullable=False)
    note = Column(Text, nullable=True)
    user_id = Column(String, default="single_user", nullable=False)
    ingest_date = Column(DateTime, default=_utcnow, nullable=False)
    updated_at = Column(DateTime, default=_utcnow, onupdate=_utcnow, nullable=False)

    document = relationship("Document", back_populates="pins")


class UserSettings(Base):
    __tablename__ = "user_settings"

    id = Column(Integer, primary_key=True)
    user_id = Column(String, default="single_user", unique=True, nullable=False)
    settings_json = Column(
        JSON,
        default=lambda: {
            "theme": "dark",
            "dashboard_cards": {
                "action_items": True,
                "costs": True,
                "documents": True,
            },
        },
    )
    updated_at = Column(DateTime, default=_utcnow, onupdate=_utcnow)


class AuditLog(Base):
    __tablename__ = "audit_logs"

    id = Column(Integer, primary_key=True)
    created_at = Column(DateTime, default=_utcnow, nullable=False, index=True)
    actor = Column(String, default="single_user", nullable=False)
    event_type = Column(SAEnum(AuditEventType), nullable=False, index=True)
    target_type = Column(String, nullable=True)
    target_id = Column(String, nullable=True)
    payload = Column(JSON, nullable=True)


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
        Index("ix_legal_costs_case_proceeding", "case_id", "proceeding_id"),
    )

    id = Column(Integer, primary_key=True, index=True)
    case_id = Column(
        String,
        ForeignKey("cases.id", ondelete="CASCADE"),
        nullable=False,
        index=True,
    )
    proceeding_id = Column(
        Integer, ForeignKey("proceedings.id"), nullable=True, index=True
    )

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

    source_document_id = Column(
        Integer,
        ForeignKey("documents.id", ondelete="SET NULL"),
        nullable=True,
    )
    # Vorschuss row points to the final-invoice row it offsets (null until reconciled)
    offsets_cost_id = Column(
        Integer,
        ForeignKey("legal_costs.id", ondelete="SET NULL"),
        nullable=True,
        index=True,
    )
    # True when this row was auto-materialized from a cost_delta signal
    auto_created = Column(Boolean, default=False, nullable=False)
    notes = Column(Text, nullable=True)
    ingest_date = Column(DateTime, default=_utcnow, nullable=False)

    case = relationship("Case")
    proceeding = relationship("Proceeding")
    source_document = relationship("Document")
    offsets_cost = relationship(
        "LegalCost",
        primaryjoin="LegalCost.offsets_cost_id == LegalCost.id",
        foreign_keys="[LegalCost.offsets_cost_id]",
        uselist=False,
    )

    @validates("case_id")
    def validate_case_id(self, key, case_id):
        return normalize_case_id(case_id)


class Conversation(Base):
    __tablename__ = "conversations"
    __table_args__ = (Index("ix_conversations_scope", "scope_type", "scope_id"),)

    id = Column(Integer, primary_key=True, index=True)
    scope_type = Column(String, nullable=False)  # "document" | "case"
    scope_id = Column(String, nullable=False)  # doc id (str) or case id (ADV-024-A)
    title = Column(String, nullable=True)
    ingest_date = Column(DateTime, default=_utcnow, nullable=False)

    messages = relationship(
        "ConversationMessage",
        back_populates="conversation",
        cascade="all, delete-orphan",
        order_by="ConversationMessage.ingest_date",
    )

    @validates("scope_id")
    def validate_scope_id(self, key, scope_id):
        if self.scope_type == "case":
            return normalize_case_id(scope_id)
        return scope_id


class ConversationMessage(Base):
    __tablename__ = "conversation_messages"
    __table_args__ = (
        Index("ix_conversation_messages_conversation", "conversation_id"),
    )

    id = Column(Integer, primary_key=True, index=True)
    conversation_id = Column(
        Integer,
        ForeignKey("conversations.id", ondelete="CASCADE"),
        nullable=False,
        index=True,
    )
    role = Column(String, nullable=False)  # "user" | "assistant"
    content = Column(Text, nullable=False)
    context_document_ids = Column(JSON, nullable=True)  # [doc_id, ...] cited
    ingest_date = Column(DateTime, default=_utcnow, nullable=False)

    conversation = relationship("Conversation", back_populates="messages")


class Entity(Base):
    """
    Extracted entities from documents, aggregated per case.

    Enables quick pivot from case to key people, organizations,
    dates, financial amounts, and legal categories.
    """

    __tablename__ = "entities"
    __table_args__ = (Index("ix_entities_case_type", "case_id", "type"),)

    id = Column(Integer, primary_key=True, index=True)
    case_id = Column(
        String,
        ForeignKey("cases.id", ondelete="CASCADE"),
        nullable=False,
        index=True,
    )

    type = Column(SAEnum(EntityType), nullable=False, index=True)
    name = Column(String, nullable=False, index=True)

    # Source tracking
    source_document_id = Column(
        Integer,
        ForeignKey("documents.id", ondelete="SET NULL"),
        nullable=True,
    )

    # Additional metadata (confidence, positions, extracted context)
    extra_data = Column(JSON, nullable=True)

    ingest_date = Column(DateTime, default=_utcnow, nullable=False)

    case = relationship("Case")
    source_document = relationship("Document")

    @validates("case_id")
    def validate_case_id(self, key, case_id):
        return normalize_case_id(case_id)
