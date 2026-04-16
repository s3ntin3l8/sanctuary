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
    UNKNOWN = "unknown"  # Neutral — for unclassified docs


class IngestStatus(enum.StrEnum):
    PENDING = "pending"
    PROCESSING = "processing"
    COMPLETED = "completed"
    FAILED = "failed"


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


class RelationshipType(enum.StrEnum):
    """How one document relates to another."""

    REPLIES_TO = "replies_to"
    REFERENCES = "references"
    ATTACHES_AS_PROOF = "attaches_as_proof"  # cited as evidence, not independent
    SUPERSEDES = "supersedes"
    CITED_BY = "cited_by"


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
    REFUTED = "refuted"
    ESTABLISHED = "established"


class ClaimEvidenceRole(enum.StrEnum):
    SUPPORTS = "supports"
    CONTESTS = "contests"
    REFUTES = "refutes"
    CITES_AS_PROOF = "cites_as_proof"


class UserReactionType(enum.StrEnum):
    """Strategic reaction captured at triage time — recalled later by AI."""

    LIES = "lies"  # 🚩
    TRUE = "true"  # ✅
    NEEDS_PROOF = "needs_proof"  # 🔍
    PRECEDENT = "precedent"  # ⚖️


class ConversationScope(enum.StrEnum):
    CASE = "case"
    DOCUMENT = "document"


class ConversationRole(enum.StrEnum):
    USER = "user"
    ASSISTANT = "assistant"
