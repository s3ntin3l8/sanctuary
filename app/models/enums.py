import enum


class CaseStatus(enum.StrEnum):
    INTAKE = "intake"
    DISCOVERY = "discovery"
    PRE_TRIAL = "pre_trial"
    TRIAL = "trial"
    POST_TRIAL = "post_trial"
    CLOSED = "closed"


class Jurisdiction(enum.StrEnum):
    """Case jurisdiction for cost system."""

    DE = "de"  # German (RVG/GKG)
    UK = "uk"  # UK
    US = "us"  # US
    OTHER = "other"


class OriginatorType(enum.StrEnum):
    """Maps to the border-l-4 originator stripes from GEMINI.md §4."""

    COURT = "court"  # Blue #0369A1 — Gavel icon
    OPPOSING = "opposing"  # Red  #B91C1C — Warning icon
    OWN = "own"  # Green #047857 — Shield icon
    THIRD_PARTY = "third_party"  # Amber #C2410C — Groups icon — Child Services,
    #     Verfahrensbeistand, Sachverständige, Jugendamt, etc.
    UNKNOWN = "unknown"  # Neutral — for unclassified docs


def parse_originator_type(value: str | None) -> "OriginatorType | None":
    """Parse a raw string into an OriginatorType, returning None on invalid input."""
    if not value:
        return None
    normalized = value.lower().strip()
    try:
        return OriginatorType(normalized)
    except ValueError:
        return None


class PipelineStage(enum.StrEnum):
    EXTRACT = "extract"
    METADATA = "metadata"
    BATCH_ANALYSIS = "batch_analysis"
    ENRICH = "enrich"
    RELATIONSHIPS = "relationships"
    CLAIMS = "claims"
    ENTITIES = "entities"
    EMBEDDINGS = "embeddings"


class StageStatus(enum.StrEnum):
    PENDING = "pending"
    RUNNING = "running"
    RETRYING = "retrying"  # last attempt failed, next attempt scheduled (in-flight)
    COMPLETED = "completed"
    FAILED = "failed"
    DISMISSED = "dismissed"
    SKIPPED = "skipped"


class PipelineState(enum.StrEnum):
    PENDING = "pending"
    RUNNING = "running"
    COMPLETED = "completed"
    FAILED = "failed"
    DISMISSED = "dismissed"
    PARTIAL = "partial"


class CostCategory(enum.StrEnum):
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


class CostStatus(enum.StrEnum):
    """Payment/reimbursement status of a cost position."""

    OFFEN = "offen"  # Due but unpaid (ausstehend)
    BEZAHLT = "bezahlt"  # Paid by us
    ERSTATTET = "erstattet"  # Reimbursed by opposing party (§91 ZPO)
    TEILWEISE = "teilweise"  # Partially paid / partially reimbursed
    STRITTIG = "strittig"  # Disputed


class EntityType(enum.StrEnum):
    """Types of entities extracted from documents."""

    PERSON = "person"
    ORGANIZATION = "organization"
    DATE = "date"
    FINANCIAL = "financial"
    LEGAL_CATEGORY = "legal_category"
    COURT = "court"
    LAW_FIRM = "law_firm"
    CITATION = "citation"


class ProceedingCourtLevel(enum.StrEnum):
    """German court hierarchy level of a proceeding."""

    AG = "ag"  # Amtsgericht
    LG = "lg"  # Landgericht
    OLG = "olg"  # Oberlandesgericht
    BGH = "bgh"  # Bundesgerichtshof
    OTHER = "other"


class ProceedingStatus(enum.StrEnum):
    ACTIVE = "active"
    CLOSED = "closed"


class DocumentRole(enum.StrEnum):
    """Structural role of a document inside its delivery bundle."""

    COVER_LETTER = "cover_letter"
    ENCLOSURE = "enclosure"
    STANDALONE = "standalone"


class DocumentType(enum.StrEnum):
    """Legal document type, assigned at ingest."""

    RULING = "ruling"  # Beschluss, Urteil
    MOTION = "motion"  # Klage, Antrag
    STATEMENT = "statement"  # Klageerwiderung, Stellungnahme
    ANNEX = "annex"  # Anlage
    RELAY = "relay"  # Begleitschreiben (court cover letter)
    CORRESPONDENCE = "correspondence"
    REPORT = "report"  # Jugendamtsbericht, Gutachten
    INVOICE = "invoice"  # Kostenrechnung
    OTHER = "other"


class SignificanceTier(enum.StrEnum):
    """Document significance to case — drives graph visibility."""

    CRITICAL = "critical"  # Decision, ruling, deadline
    SIGNIFICANT = "significant"  # Substantive statement, motion
    INFORMATIONAL = "informational"  # Factual update, acknowledgment
    ADMINISTRATIVE = "administrative"  # Pure relay, receipt confirmation


class IngestBatchSourceType(enum.StrEnum):
    """Where a batch of documents came from."""

    EMAIL = "email"
    SCAN = "scan"
    MANUAL = "manual"


class IngestBatchStatus(enum.StrEnum):
    PENDING = "pending"
    PROCESSING = "processing"
    COMPLETED = "completed"
    FAILED = "failed"
    DISMISSED = "dismissed"
    AWAITING_SLICING = "awaiting_slicing"


class RelationshipType(enum.StrEnum):
    """How one document relates to another."""

    REPLIES_TO = "replies_to"
    REFERENCES = "references"
    ATTACHES_AS_PROOF = "attaches_as_proof"  # cited as evidence, not independent
    SUPERSEDES = "supersedes"
    CITED_BY = "cited_by"
    ENCLOSES = "encloses"  # cover-letter → enclosure within a single ingest batch


class RelationshipConfidence(enum.StrEnum):
    """Provenance of a document relationship."""

    AI_DETECTED = "ai_detected"
    USER_CONFIRMED = "user_confirmed"
    USER_CREATED = "user_created"


class ActionItemType(enum.StrEnum):
    """Type of action derived from a document."""

    DEADLINE = "deadline"  # Frist — must respond by a date
    COURT_DATE = "court_date"  # Verhandlungstermin, Anhörung
    RESPONSE_REQUIRED = "response_required"  # Stellungnahme erwartet
    FILING_REQUIRED = "filing_required"  # Schriftsatz einzureichen
    PAYMENT_DUE = (
        "payment_due"  # Zahlungsfrist — Gerichtskostenrechnung, Landesjustizkasse
    )


class ActionItemStatus(enum.StrEnum):
    OPEN = "open"
    COMPLETED = "completed"
    DISMISSED = "dismissed"


class ClaimType(enum.StrEnum):
    FACTUAL = "factual"
    LEGAL = "legal"
    PROCEDURAL = "procedural"


class ClaimStatus(enum.StrEnum):
    ASSERTED = "asserted"
    CONTESTED = "contested"
    NEEDS_PROOF = "needs_proof"
    REFUTED = "refuted"
    ESTABLISHED = "established"


class ClaimEvidenceRole(enum.StrEnum):
    ASSERTS = "asserts"  # the document that originally made the claim
    SUPPORTS = "supports"
    CONTESTS = "contests"
    REFUTES = "refutes"
    CITES_AS_PROOF = "cites_as_proof"


class ProposalStatus(enum.StrEnum):
    """Lifecycle of an AI-generated proposal awaiting user review."""

    PENDING = "pending"
    CONFIRMED = "confirmed"
    DISMISSED = "dismissed"


class ProposalConfidence(enum.StrEnum):
    """How sure the AI is. Drives auto-vs-pending behavior in some flows."""

    HIGH = "high"
    MEDIUM = "medium"
    LOW = "low"


class UserReactionType(enum.StrEnum):
    """Strategic reaction captured at triage time — recalled later by AI."""

    LIES = "lies"  # 🚩
    TRUE = "true"  # ✅
    NEEDS_PROOF = "needs_proof"  # 🔍
    PRECEDENT = "precedent"  # ⚖️


class DocumentStatus(enum.StrEnum):
    ACTIVE = "active"
    DISMISSED = "dismissed"


class CaseType(enum.StrEnum):
    """Legal domain of the case — drives cost-allocation defaults."""

    CIVIL = "civil"
    FAMILY = "family"
    ADMINISTRATIVE = "administrative"
    CRIMINAL = "criminal"
