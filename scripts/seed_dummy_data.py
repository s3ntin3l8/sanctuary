"""Comprehensive triage seed — one of every triage state, bundle shape, and edge case.

Run:  make seed-adv

Bundles seeded (all scoped to ADV-024-A / ADV-031-B / ADV-100-X)
─────────────────────────────────────────────────────────────────
 1  CLEAN         All docs confirmed; Confirm bundle → active; proceeding chip visible
 2  PARTIAL       First doc ✓, others still need review; CTA disabled; suggested_case_id chip
 3  FRESH         All need review; all 4 pipeline states across 4 docs
 4  PROOF_PILL    ATTACHES_AS_PROOF edge → [proof] badge; ActionItem parked under _TRIAGE
 5  MULTI_ROOT    One email → 2 cover-letter subtrees (Bundle A / Bundle B segmentation)
 6  DEEP_NEST     Cover → child → grandchild (depth-2 L-connector indentation)
 7  CRITICAL_SCAN CRITICAL ruling via scanner; floats to top of feed; ai_summary=failed pill
 8  LOW_CONF      UNKNOWN originator; all-low extraction_confidence; all fields expanded
 9  REACTIONS     All four UserReactionType values pre-seeded on docs
10  SYNTHETIC     Loose doc (no batch) → synthetic bundle
11  COMPLETED     batch.status=COMPLETED → must NOT appear in triage feed
"""

import os
import sys
from datetime import UTC, datetime, timedelta
from pathlib import Path

sys.path.append(str(Path(__file__).resolve().parent.parent))
os.environ.setdefault("SQLALCHEMY_DATABASE_URL", "sqlite:///./data/sanctuary.db")

from app.config import SessionLocal, engine
from app.models.database import (
    ActionItem,
    Base,
    Case,
    CaseStatus,
    Document,
    DocumentRelationship,
    DocumentRole,
    DocumentType,
    IngestBatch,
    IngestBatchSourceType,
    IngestBatchStatus,
    IngestStatus,
    OriginatorType,
    Proceeding,
    SignificanceTier,
    UserReaction,
)
from app.models.enums import (
    ActionItemStatus,
    ActionItemType,
    Jurisdiction,
    ProceedingCourtLevel,
    ProceedingStatus,
    RelationshipConfidence,
    RelationshipType,
    UserReactionType,
)

Base.metadata.create_all(bind=engine, checkfirst=True)

from alembic import command as _alembic_command
from alembic.config import Config as _AlembicConfig

_alembic_command.stamp(_AlembicConfig("alembic.ini"), "head")

db = SessionLocal()
now = datetime.now(UTC).replace(second=0, microsecond=0)

# ── Idempotency: remove prior seed data ────────────────────────────────────
# Delete in dependency order so FK constraints aren't violated.
SEED_CASE_IDS = ["_TRIAGE", "ADV-024-A", "ADV-031-B", "ADV-100-X"]

for model in (
    UserReaction,
    ActionItem,
    DocumentRelationship,
    Document,
    IngestBatch,
    Proceeding,
):
    db.query(model).filter(
        model.case_id.in_(SEED_CASE_IDS)
        if hasattr(model, "case_id")
        else model.id.isnot(None)  # fallback — unused in practice here
    ).delete(synchronize_session=False)

# DocumentRelationship has no case_id; delete via joined doc IDs instead.
# (The delete above skipped it via the fallback — clear it properly.)
db.query(DocumentRelationship).filter(
    DocumentRelationship.from_document_id.in_(
        db.query(Document.id).filter(Document.case_id.in_(SEED_CASE_IDS))
    )
).delete(synchronize_session=False)
db.query(UserReaction).filter(
    UserReaction.document_id.in_(
        db.query(Document.id).filter(Document.case_id.in_(SEED_CASE_IDS))
    )
).delete(synchronize_session=False)
db.query(Document).filter(Document.case_id.in_(SEED_CASE_IDS)).delete(
    synchronize_session=False
)
db.query(IngestBatch).filter(IngestBatch.case_id.in_(SEED_CASE_IDS)).delete(
    synchronize_session=False
)
# Orphaned batches (case_id=None) from previous runs — identify by subject prefix.
db.query(IngestBatch).filter(IngestBatch.subject.like("[SEED]%")).delete(
    synchronize_session=False
)
db.query(Proceeding).filter(Proceeding.case_id.in_(SEED_CASE_IDS)).delete(
    synchronize_session=False
)
db.query(Case).filter(Case.id.in_(SEED_CASE_IDS)).delete(synchronize_session=False)
db.commit()

# ── Cases ──────────────────────────────────────────────────────────────────
triage_inbox = Case(
    id="_TRIAGE",
    title="Triage Inbox",
    status=CaseStatus.INTAKE,
    jurisdiction=Jurisdiction.DE,
)
case_a = Case(
    id="ADV-024-A",
    title="Vane v. Vane — Elternsorge AG Hamburg",
    status=CaseStatus.DISCOVERY,
    jurisdiction=Jurisdiction.DE,
)
case_b = Case(
    id="ADV-031-B",
    title="Meridian Holdings v. Stadtplanung Berlin",
    status=CaseStatus.PRE_TRIAL,
    jurisdiction=Jurisdiction.DE,
)
case_c = Case(
    id="ADV-100-X",
    title="DataBreach GmbH — Corporate Litigation",
    status=CaseStatus.DISCOVERY,
    jurisdiction=Jurisdiction.DE,
)
db.add_all([triage_inbox, case_a, case_b, case_c])
db.commit()

# ── Proceedings ────────────────────────────────────────────────────────────
proc_a = Proceeding(
    case_id="ADV-024-A",
    court_name="Amtsgericht Hamburg",
    court_level=ProceedingCourtLevel.AG,
    subject_matter="§ 1671 BGB — Elterliche Sorge",
    az_court="003 F 426/25",
    status=ProceedingStatus.ACTIVE,
    started_at=now - timedelta(days=180),
)
proc_b = Proceeding(
    case_id="ADV-031-B",
    court_name="Landgericht Berlin",
    court_level=ProceedingCourtLevel.LG,
    subject_matter="§ 823 BGB — Schadensersatz",
    az_court="14 O 123/25",
    status=ProceedingStatus.ACTIVE,
    started_at=now - timedelta(days=90),
)
db.add_all([proc_a, proc_b])
db.commit()


# ── Bundle helpers ──────────────────────────────────────────────────────────


def make_batch(
    source_type,
    subject,
    *,
    case_id=None,
    proceeding_id=None,
    status=IngestBatchStatus.PENDING,
    sender_email=None,
    days_ago=1,
):
    batch = IngestBatch(
        source_type=source_type,
        subject=f"[SEED] {subject}",
        case_id=case_id,
        proceeding_id=proceeding_id,
        status=status,
        sender_email=sender_email,
        received_at=now - timedelta(days=days_ago),
    )
    db.add(batch)
    db.flush()
    return batch


def make_doc(
    title,
    *,
    batch,
    role=DocumentRole.COVER_LETTER,
    originator=OriginatorType.COURT,
    sender="gericht@ag-hamburg.de",
    case_id="_TRIAGE",
    parent_id=None,
    needs_review=True,
    review_reasons=None,
    significance=SignificanceTier.INFORMATIONAL,
    court_relay=False,
    attributed_originator=None,
    doc_type=DocumentType.RELAY,
    ingest_status=IngestStatus.COMPLETED,
    ai_summary_status="generated",
    ai_summary=None,
    key_passages=None,
    cost_candidates=None,
    extraction_confidence=None,
    days_ago=1,
):
    if review_reasons is None:
        review_reasons = ["missing_case_id"] if needs_review else []
    if extraction_confidence is None:
        extraction_confidence = {
            "sender": "high",
            "date": "high",
            "case_id": "high",
            "originator": "high",
        }

    doc = Document(
        title=title,
        content=_content(title, originator, sender),
        case_id=case_id,
        ingest_batch_id=batch.id if batch else None,
        parent_id=parent_id,
        role=role,
        originator_type=originator,
        sender=sender,
        received_date=now - timedelta(days=days_ago),
        needs_review=needs_review,
        review_reasons=review_reasons,
        significance_tier=significance,
        court_relay=court_relay,
        attributed_originator=attributed_originator,
        document_type=doc_type,
        ingest_status=ingest_status,
        ai_summary_status=ai_summary_status,
        ai_summary=ai_summary,
        key_passages=key_passages,
        cost_candidates=cost_candidates,
        extraction_confidence=extraction_confidence,
        created_at=now - timedelta(days=days_ago),
    )
    db.add(doc)
    db.flush()
    return doc


def _content(title, originator, sender):
    originator_label = {
        OriginatorType.COURT: "Gericht",
        OriginatorType.OPPOSING: "Gegenseite",
        OriginatorType.OWN: "Eigene Kanzlei",
        OriginatorType.THIRD_PARTY: "Dritte",
        OriginatorType.UNKNOWN: "Unbekannt",
    }.get(originator, "Unbekannt")
    return (
        f"# {title}\n\n"
        f"**Von:** {sender}  \n"
        f"**Kategorie:** {originator_label}\n\n"
        "## Inhalt\n\n"
        "Dieser Schriftsatz dient als Testdokument für die Triage-Ansicht. "
        "Er enthält ausreichend Text, damit der Lesebereich befüllt wirkt.\n\n"
        "> Zitat aus dem Schriftsatz zur Demonstration der Key-Passage-Hervorhebung.\n\n"
        "## Rechtliche Ausführungen\n\n"
        "Nach §1671 BGB ist die elterliche Sorge bei Vorliegen der gesetzlichen "
        "Voraussetzungen neu zu regeln. Die Parteien streiten über die tatsächlichen "
        "Voraussetzungen.\n\n"
        "Streitwert: **3.000,00 EUR**"
    )


# ═══════════════════════════════════════════════════════════════════════════
# BUNDLE 1 — CLEAN: all docs confirmed, Confirm bundle → active
#   Tests: enabled CTA, suggested_case_id chip [ADV-024-A?], proceeding chip
# ═══════════════════════════════════════════════════════════════════════════
b1 = make_batch(
    IngestBatchSourceType.EMAIL,
    "AG Hamburg — Gerichtsbeschluss (alle Metadaten bestätigt)",
    sender_email="geschaeftsstelle@ag-hamburg.de",
    days_ago=3,
)

b1_cover = make_doc(
    "Begleitschreiben AG Hamburg — Beschluss §1671",
    batch=b1,
    role=DocumentRole.COVER_LETTER,
    originator=OriginatorType.COURT,
    sender="geschaeftsstelle@ag-hamburg.de",
    case_id="ADV-024-A",  # AI already suggested this case
    needs_review=False,
    review_reasons=[],
    significance=SignificanceTier.INFORMATIONAL,
    court_relay=True,
    attributed_originator="Richter Hoffmann",
    doc_type=DocumentType.RELAY,
    ai_summary_status="generated",
    ai_summary={
        "legal_significance": "Gerichtliche Zustellung eines Beschlusses nach §1671 BGB.",
        "required_action": "Stellungnahme binnen zwei Wochen einreichen.",
        "financial_impact": "Keine direkten Kostenauswirkungen.",
    },
    key_passages=[
        {
            "text": "Stellungnahme binnen zwei Wochen",
            "rationale": "Response deadline",
            "span": [0, 30],
        },
    ],
)

b1_ruling = make_doc(
    "Beschluss AG Hamburg — Elterliche Sorge §1671 BGB",
    batch=b1,
    role=DocumentRole.ENCLOSURE,
    originator=OriginatorType.COURT,
    sender="geschaeftsstelle@ag-hamburg.de",
    case_id="ADV-024-A",
    parent_id=b1_cover.id,
    needs_review=False,
    review_reasons=[],
    significance=SignificanceTier.CRITICAL,
    doc_type=DocumentType.RULING,
    ai_summary_status="generated",
    ai_summary={
        "legal_significance": "Beschluss über elterliche Sorge. Entscheidung zugunsten der Mutter.",
        "required_action": "Frist zur Beschwerde: 4 Wochen ab Zustellung (§63 FamFG).",
        "financial_impact": "Gerichtskosten EUR 231,00.",
    },
    key_passages=[
        {
            "text": "Die elterliche Sorge wird der Mutter übertragen",
            "rationale": "Core ruling",
            "span": [0, 52],
        },
        {
            "text": "Frist zur Beschwerde: 4 Wochen",
            "rationale": "Deadline",
            "span": [100, 130],
        },
    ],
    cost_candidates=[
        {"type": "amount", "value": 231.00, "context": "Gerichtskosten EUR 231,00"},
    ],
)

# Wire the batch proceeding so the proceeding chip renders
b1.proceeding_id = proc_a.id
db.flush()

db.add(
    ActionItem(
        case_id="ADV-024-A",
        source_document_id=b1_cover.id,
        title="Stellungnahme zum Beschluss §1671",
        description="Stellungnahme binnen zwei Wochen auf den Beschluss einreichen.",
        due_date=now + timedelta(days=14),
        action_type=ActionItemType.DEADLINE,
        status=ActionItemStatus.OPEN,
    )
)
db.commit()


# ═══════════════════════════════════════════════════════════════════════════
# BUNDLE 2 — PARTIAL: first doc confirmed ✓, two still need review
#   Tests: mixed card states, CTA disabled (2 docs), suggested_case_id chip
# ═══════════════════════════════════════════════════════════════════════════
b2 = make_batch(
    IngestBatchSourceType.EMAIL,
    "Kanzlei Müller — Klageerwiderung + Anlagen (teilweise bestätigt)",
    sender_email="mueller@kanzlei-gegenseite.de",
    days_ago=1,
)

b2_cover = make_doc(
    "Begleitschreiben AG Hamburg — Klageerwiderung",
    batch=b2,
    role=DocumentRole.COVER_LETTER,
    originator=OriginatorType.COURT,
    sender="geschaeftsstelle@ag-hamburg.de",
    case_id="ADV-024-A",  # suggested by AI
    needs_review=False,
    review_reasons=[],
    significance=SignificanceTier.INFORMATIONAL,
    court_relay=True,
    attributed_originator="Dr. Müller, Rechtsanwalt",
    doc_type=DocumentType.RELAY,
    ai_summary_status="generated",
)

b2_statement = make_doc(
    "Klageerwiderung — Dr. Müller für Beklagten",
    batch=b2,
    role=DocumentRole.ENCLOSURE,
    originator=OriginatorType.OPPOSING,
    sender=None,  # missing_sender
    case_id="ADV-024-A",
    parent_id=b2_cover.id,
    needs_review=True,
    review_reasons=["missing_sender"],
    significance=SignificanceTier.SIGNIFICANT,
    doc_type=DocumentType.STATEMENT,
    ai_summary_status="generated",
    extraction_confidence={
        "sender": "low",
        "date": "high",
        "case_id": "high",
        "originator": "high",
    },
    cost_candidates=[
        {"type": "amount", "value": 5000.00, "context": "Streitwert: 5.000,00 EUR"},
        {
            "type": "rvg_position",
            "value": "Nr. 3100 VV RVG",
            "context": "Verfahrensgebühr",
        },
    ],
)

b2_annex = make_doc(
    "Anlage B1 — Kindergarten-Quittungen",
    batch=b2,
    role=DocumentRole.ENCLOSURE,
    originator=OriginatorType.OPPOSING,
    sender="mueller@kanzlei-gegenseite.de",
    case_id="_TRIAGE",  # AI didn't catch the case here
    parent_id=b2_cover.id,
    needs_review=True,
    review_reasons=["missing_case_id", "missing_received_date"],
    significance=SignificanceTier.INFORMATIONAL,
    doc_type=DocumentType.ANNEX,
    ai_summary_status="pending",
    extraction_confidence={
        "sender": "medium",
        "date": "low",
        "case_id": "low",
        "originator": "medium",
    },
)
db.commit()


# ═══════════════════════════════════════════════════════════════════════════
# BUNDLE 3 — FRESH: all need review, all 4 pipeline states visible
#   Tests: ⏳ pending / ⚙ AI processing / ✓ ready / ⚠ failed
# ═══════════════════════════════════════════════════════════════════════════
b3 = make_batch(
    IngestBatchSourceType.EMAIL,
    "LG Berlin — Klageschrift (frische Einlieferung, alle Pipelines sichtbar)",
    sender_email="eingang@lg-berlin.de",
    days_ago=0,
)

b3_cover = make_doc(
    "Begleitschreiben LG Berlin — Klageschrift eingegangen",
    batch=b3,
    role=DocumentRole.COVER_LETTER,
    originator=OriginatorType.COURT,
    sender="eingang@lg-berlin.de",
    case_id="_TRIAGE",
    needs_review=True,
    review_reasons=["missing_case_id"],
    significance=SignificanceTier.INFORMATIONAL,
    court_relay=True,
    doc_type=DocumentType.RELAY,
    ingest_status=IngestStatus.PENDING,
    ai_summary_status="pending",
    # ⏳ pending: ingest not yet run
)

b3_motion = make_doc(
    "Klageschrift — Meridian Holdings v. Stadtplanung",
    batch=b3,
    role=DocumentRole.ENCLOSURE,
    originator=OriginatorType.OWN,
    sender="kanzlei@sanctuary-counsel.de",
    case_id="_TRIAGE",
    parent_id=b3_cover.id,
    needs_review=True,
    review_reasons=["missing_case_id"],
    significance=SignificanceTier.CRITICAL,
    doc_type=DocumentType.MOTION,
    ingest_status=IngestStatus.COMPLETED,
    ai_summary_status="pending",
    # ⚙ AI processing: docling done, AI not yet
)

b3_exhibit = make_doc(
    "Anlage K1 — Bebauungsplan-Gutachten",
    batch=b3,
    role=DocumentRole.ENCLOSURE,
    originator=OriginatorType.THIRD_PARTY,
    sender="gutachter@stadtplanung.de",
    case_id="_TRIAGE",
    parent_id=b3_motion.id,
    needs_review=True,
    review_reasons=["missing_case_id"],
    significance=SignificanceTier.SIGNIFICANT,
    doc_type=DocumentType.REPORT,
    ingest_status=IngestStatus.COMPLETED,
    ai_summary_status="generated",
    # ✓ ready: full pipeline done
    ai_summary={
        "legal_significance": "Sachverständigengutachten zum Bebauungsplan.",
        "required_action": "Auf Aussagen zur Erschließung eingehen.",
        "financial_impact": "Streitwert ca. EUR 120.000.",
    },
    key_passages=[
        {
            "text": "Der Bebauungsplan verstößt gegen §34 BauGB",
            "rationale": "Key legal finding",
            "span": [0, 44],
        },
    ],
)

b3_invoice = make_doc(
    "Kostenrechnung Gericht — Einreichungsgebühr",
    batch=b3,
    role=DocumentRole.ENCLOSURE,
    originator=OriginatorType.COURT,
    sender="eingang@lg-berlin.de",
    case_id="_TRIAGE",
    needs_review=True,
    review_reasons=["missing_case_id"],
    significance=SignificanceTier.ADMINISTRATIVE,
    doc_type=DocumentType.INVOICE,
    ingest_status=IngestStatus.FAILED,
    ai_summary_status="failed",
    # ⚠ failed: ingestion error
)
db.commit()


# ═══════════════════════════════════════════════════════════════════════════
# BUNDLE 4 — PROOF_PILL: ATTACHES_AS_PROOF edge + ActionItem cascade test
#   Tests: [proof] badge on annex; ActionItem(case_id=_TRIAGE) → cascades on confirm
# ═══════════════════════════════════════════════════════════════════════════
b4 = make_batch(
    IngestBatchSourceType.EMAIL,
    "Anwalt Schneider — Stellungnahme + Beweis-Anlage",
    sender_email="schneider@kanzlei-schneider.de",
    days_ago=2,
)

b4_cover = make_doc(
    "Begleitschreiben LG Berlin — Stellungnahme Beklagter",
    batch=b4,
    role=DocumentRole.COVER_LETTER,
    originator=OriginatorType.COURT,
    sender="eingang@lg-berlin.de",
    case_id="_TRIAGE",
    needs_review=True,
    review_reasons=["missing_case_id"],
    court_relay=True,
    significance=SignificanceTier.INFORMATIONAL,
    doc_type=DocumentType.RELAY,
    ai_summary_status="generated",
)

b4_statement = make_doc(
    "Stellungnahme Beklagter — Schneider",
    batch=b4,
    role=DocumentRole.ENCLOSURE,
    originator=OriginatorType.OPPOSING,
    sender="schneider@kanzlei-schneider.de",
    case_id="_TRIAGE",
    parent_id=b4_cover.id,
    needs_review=True,
    review_reasons=["missing_case_id"],
    significance=SignificanceTier.SIGNIFICANT,
    doc_type=DocumentType.STATEMENT,
    ai_summary_status="generated",
    key_passages=[
        {
            "text": "Der Beklagte war am 10.01.2026 ortsabwesend",
            "rationale": "Contested fact",
            "span": [0, 44],
        },
    ],
)

b4_proof = make_doc(
    "Anlage S1 — Reisekostenabrechnung als Nachweis",
    batch=b4,
    role=DocumentRole.ENCLOSURE,
    originator=OriginatorType.OPPOSING,
    sender="schneider@kanzlei-schneider.de",
    case_id="_TRIAGE",
    parent_id=b4_cover.id,
    needs_review=True,
    review_reasons=["missing_case_id"],
    significance=SignificanceTier.INFORMATIONAL,
    doc_type=DocumentType.ANNEX,
    ai_summary_status="generated",
    extraction_confidence={
        "sender": "medium",
        "date": "medium",
        "case_id": "low",
        "originator": "medium",
    },
    cost_candidates=[
        {"type": "amount", "value": 342.50, "context": "Reisekosten 342,50 EUR"},
    ],
)

db.add(
    DocumentRelationship(
        from_document_id=b4_statement.id,
        to_document_id=b4_proof.id,
        relationship_type=RelationshipType.ATTACHES_AS_PROOF,
        confidence=RelationshipConfidence.AI_DETECTED,
    )
)

# ActionItem parked under _TRIAGE — should cascade on bundle confirm
db.add(
    ActionItem(
        case_id="_TRIAGE",
        source_document_id=b4_cover.id,
        title="Erwiderung auf Stellungnahme Schneider",
        description="Frist zur Gegendarstellung: 2 Wochen.",
        due_date=now + timedelta(days=14),
        action_type=ActionItemType.DEADLINE,
        status=ActionItemStatus.OPEN,
    )
)
db.commit()


# ═══════════════════════════════════════════════════════════════════════════
# BUNDLE 5 — MULTI_ROOT: one email, two cover-letter subtrees
#   Tests: parent_groups yields 2 groups → Bundle A / Bundle B labels
# ═══════════════════════════════════════════════════════════════════════════
b5 = make_batch(
    IngestBatchSourceType.EMAIL,
    "AG Hamburg — Klageerwiderung + Jugendamtsbericht (eine E-Mail, zwei Gruppen)",
    sender_email="geschaeftsstelle@ag-hamburg.de",
    days_ago=0,
)

# Root A: Klageerwiderung group
b5_cover_a = make_doc(
    "Begleitschreiben A — Klageerwiderung",
    batch=b5,
    role=DocumentRole.COVER_LETTER,
    originator=OriginatorType.COURT,
    sender="geschaeftsstelle@ag-hamburg.de",
    case_id="_TRIAGE",
    needs_review=True,
    review_reasons=["missing_case_id"],
    court_relay=True,
    attributed_originator="Dr. Müller, Rechtsanwalt",
    significance=SignificanceTier.INFORMATIONAL,
    doc_type=DocumentType.RELAY,
    ai_summary_status="generated",
)

b5_statement = make_doc(
    "Klageerwiderung Beklagter — Dr. Müller",
    batch=b5,
    role=DocumentRole.ENCLOSURE,
    originator=OriginatorType.OPPOSING,
    sender="mueller@kanzlei-gegenseite.de",
    case_id="_TRIAGE",
    parent_id=b5_cover_a.id,
    needs_review=True,
    review_reasons=["missing_case_id"],
    significance=SignificanceTier.SIGNIFICANT,
    doc_type=DocumentType.STATEMENT,
    ai_summary_status="generated",
    key_passages=[
        {
            "text": "§1671 BGB ist nicht gerechtfertigt",
            "rationale": "Core legal argument",
            "span": [0, 34],
        },
    ],
)

b5_annex = make_doc(
    "Anlage K1 — Betreuungsrechnung",
    batch=b5,
    role=DocumentRole.ENCLOSURE,
    originator=OriginatorType.OPPOSING,
    sender="mueller@kanzlei-gegenseite.de",
    case_id="_TRIAGE",
    parent_id=b5_cover_a.id,
    needs_review=True,
    review_reasons=["missing_case_id"],
    significance=SignificanceTier.INFORMATIONAL,
    doc_type=DocumentType.ANNEX,
    ai_summary_status="generated",
    cost_candidates=[
        {"type": "amount", "value": 505.00, "context": "Betreuungskosten 505,00 EUR"},
    ],
)

# Root B: Jugendamtsbericht group (separate subtree in same batch)
b5_cover_b = make_doc(
    "Begleitschreiben B — Jugendamtsbericht",
    batch=b5,
    role=DocumentRole.COVER_LETTER,
    originator=OriginatorType.COURT,
    sender="geschaeftsstelle@ag-hamburg.de",
    case_id="_TRIAGE",
    needs_review=True,
    review_reasons=["missing_case_id"],
    court_relay=True,
    attributed_originator="Jugendamt Hamburg",
    significance=SignificanceTier.ADMINISTRATIVE,
    doc_type=DocumentType.RELAY,
    ai_summary_status="generated",
)

b5_report = make_doc(
    "Jugendamtsbericht — Kindeswohl §50 SGB VIII",
    batch=b5,
    role=DocumentRole.ENCLOSURE,
    originator=OriginatorType.THIRD_PARTY,
    sender="jugendamt@hamburg.de",
    case_id="_TRIAGE",
    parent_id=b5_cover_b.id,
    needs_review=True,
    review_reasons=["missing_case_id"],
    significance=SignificanceTier.SIGNIFICANT,
    doc_type=DocumentType.REPORT,
    ai_summary_status="generated",
    key_passages=[
        {
            "text": "Empfehlung: gemeinsame elterliche Sorge beibehalten",
            "rationale": "Agency recommendation",
            "span": [0, 50],
        },
    ],
)

db.add(
    ActionItem(
        case_id="_TRIAGE",
        source_document_id=b5_cover_a.id,
        title="Stellungnahme auf Klageerwiderung",
        description="Frist 30 Tage ab Zustellung.",
        due_date=now + timedelta(days=30),
        action_type=ActionItemType.DEADLINE,
        status=ActionItemStatus.OPEN,
    )
)
db.commit()


# ═══════════════════════════════════════════════════════════════════════════
# BUNDLE 6 — DEEP_NEST: depth-2 indentation (cover → child → grandchild)
#   Tests: L-connector svg at depth=1 and depth=2
# ═══════════════════════════════════════════════════════════════════════════
b6 = make_batch(
    IngestBatchSourceType.SCAN,
    "Eingang Scan — Schriftsatz mit verschachtelten Anlagen",
    days_ago=4,
)

b6_cover = make_doc(
    "Hauptschriftsatz — Antragstellung",
    batch=b6,
    role=DocumentRole.COVER_LETTER,
    originator=OriginatorType.OWN,
    sender="kanzlei@sanctuary-counsel.de",
    case_id="_TRIAGE",
    needs_review=True,
    review_reasons=["missing_case_id"],
    significance=SignificanceTier.SIGNIFICANT,
    doc_type=DocumentType.MOTION,
    ai_summary_status="generated",
)

b6_child = make_doc(
    "Anlage A — Gutachten (Tiefe 1)",
    batch=b6,
    role=DocumentRole.ENCLOSURE,
    originator=OriginatorType.THIRD_PARTY,
    sender="gutachter@expert.de",
    case_id="_TRIAGE",
    parent_id=b6_cover.id,
    needs_review=True,
    review_reasons=["missing_case_id"],
    significance=SignificanceTier.INFORMATIONAL,
    doc_type=DocumentType.REPORT,
    ai_summary_status="generated",
)

b6_grandchild = make_doc(
    "Anlage A.1 — Rohdaten zum Gutachten (Tiefe 2)",
    batch=b6,
    role=DocumentRole.ENCLOSURE,
    originator=OriginatorType.THIRD_PARTY,
    sender="gutachter@expert.de",
    case_id="_TRIAGE",
    parent_id=b6_child.id,
    needs_review=True,
    review_reasons=["missing_case_id"],
    significance=SignificanceTier.ADMINISTRATIVE,
    doc_type=DocumentType.ANNEX,
    ai_summary_status="pending",
    extraction_confidence={
        "sender": "medium",
        "date": "low",
        "case_id": "low",
        "originator": "medium",
    },
)
db.commit()


# ═══════════════════════════════════════════════════════════════════════════
# BUNDLE 7 — CRITICAL_SCAN: CRITICAL doc, ai_summary failed, floats to top
#   Tests: scanner source icon; failed pill; urgency-first sort (most review flags)
# ═══════════════════════════════════════════════════════════════════════════
b7 = make_batch(
    IngestBatchSourceType.SCAN,
    "Eingang Scan — Urteil (KRITISCH, OCR-Fehler)",
    days_ago=0,
)

b7_ruling = make_doc(
    "Urteil AG Hamburg — §1671 BGB (KRITISCH)",
    batch=b7,
    role=DocumentRole.STANDALONE,
    originator=OriginatorType.COURT,
    sender="geschaeftsstelle@ag-hamburg.de",
    case_id="_TRIAGE",
    needs_review=True,
    review_reasons=["missing_case_id", "missing_received_date"],
    significance=SignificanceTier.CRITICAL,
    doc_type=DocumentType.RULING,
    ingest_status=IngestStatus.FAILED,
    ai_summary_status="failed",
    extraction_confidence={
        "sender": "low",
        "date": "low",
        "case_id": "low",
        "originator": "low",
    },
)
db.commit()


# ═══════════════════════════════════════════════════════════════════════════
# BUNDLE 8 — LOW_CONF: UNKNOWN originator, all-low confidence → all fields open
#   Tests: unknown originator chip color; metadata form shows all fields expanded
# ═══════════════════════════════════════════════════════════════════════════
b8 = make_batch(
    IngestBatchSourceType.MANUAL,
    "Manuell eingepflegtes Dokument — Absender unbekannt",
    days_ago=5,
)

b8_doc = make_doc(
    "Unbekanntes Schriftstück — Herkunft unklar",
    batch=b8,
    role=DocumentRole.STANDALONE,
    originator=OriginatorType.UNKNOWN,
    sender=None,
    case_id="_TRIAGE",
    needs_review=True,
    review_reasons=[
        "missing_case_id",
        "missing_sender",
        "missing_originator",
        "missing_received_date",
    ],
    significance=SignificanceTier.INFORMATIONAL,
    doc_type=DocumentType.OTHER,
    ai_summary_status="pending",
    extraction_confidence={
        "sender": "low",
        "date": "low",
        "case_id": "low",
        "originator": "low",
    },
)
db.commit()


# ═══════════════════════════════════════════════════════════════════════════
# BUNDLE 9 — REACTIONS: all four UserReactionType values pre-seeded
#   Tests: reaction pills visible on cards and in the HUD reaction bar
# ═══════════════════════════════════════════════════════════════════════════
b9 = make_batch(
    IngestBatchSourceType.EMAIL,
    "Gegenseite — Behauptungsschriftsatz (Reaktionen vorbelegt)",
    sender_email="gegenseite@kanzlei-opposition.de",
    days_ago=7,
)

b9_cover = make_doc(
    "Begleitschreiben — Behauptungsschriftsatz",
    batch=b9,
    role=DocumentRole.COVER_LETTER,
    originator=OriginatorType.COURT,
    sender="eingang@ag-hamburg.de",
    case_id="_TRIAGE",
    needs_review=True,
    review_reasons=["missing_case_id"],
    court_relay=True,
    significance=SignificanceTier.INFORMATIONAL,
    doc_type=DocumentType.RELAY,
    ai_summary_status="generated",
)

b9_lies = make_doc(
    "Tatsachenbehauptung — Behauptete Abwesenheit am 10.01.2026",
    batch=b9,
    role=DocumentRole.ENCLOSURE,
    originator=OriginatorType.OPPOSING,
    sender="gegenseite@kanzlei-opposition.de",
    case_id="_TRIAGE",
    parent_id=b9_cover.id,
    needs_review=True,
    review_reasons=["missing_case_id"],
    significance=SignificanceTier.SIGNIFICANT,
    doc_type=DocumentType.STATEMENT,
    ai_summary_status="generated",
    key_passages=[
        {
            "text": "Der Beklagte war am 10.01.2026 ortsabwesend",
            "rationale": "Contested fact — see reaction",
            "span": [0, 44],
        },
    ],
)

b9_needs_proof = make_doc(
    "Behauptung ohne Beleg — Unterhaltsrückstände",
    batch=b9,
    role=DocumentRole.ENCLOSURE,
    originator=OriginatorType.OPPOSING,
    sender="gegenseite@kanzlei-opposition.de",
    case_id="_TRIAGE",
    parent_id=b9_cover.id,
    needs_review=True,
    review_reasons=["missing_case_id"],
    significance=SignificanceTier.SIGNIFICANT,
    doc_type=DocumentType.STATEMENT,
    ai_summary_status="generated",
)

b9_precedent = make_doc(
    "Verweis auf BGH-Rechtsprechung — §1671 BGB",
    batch=b9,
    role=DocumentRole.ENCLOSURE,
    originator=OriginatorType.OPPOSING,
    sender="gegenseite@kanzlei-opposition.de",
    case_id="_TRIAGE",
    parent_id=b9_cover.id,
    needs_review=True,
    review_reasons=["missing_case_id"],
    significance=SignificanceTier.INFORMATIONAL,
    doc_type=DocumentType.CORRESPONDENCE,
    ai_summary_status="generated",
)

b9_true = make_doc(
    "Bestätigte Tatsache — Kindergartenbeitrag März belegt",
    batch=b9,
    role=DocumentRole.ENCLOSURE,
    originator=OriginatorType.THIRD_PARTY,
    sender="kindergarten@hamburg.de",
    case_id="_TRIAGE",
    parent_id=b9_cover.id,
    needs_review=True,
    review_reasons=["missing_case_id"],
    significance=SignificanceTier.INFORMATIONAL,
    doc_type=DocumentType.INVOICE,
    ai_summary_status="generated",
)

db.add_all(
    [
        UserReaction(
            document_id=b9_lies.id,
            reaction=UserReactionType.LIES,
            notes="Widerspricht Reisekostenabrechnung Anlage S1",
        ),
        UserReaction(
            document_id=b9_needs_proof.id, reaction=UserReactionType.NEEDS_PROOF
        ),
        UserReaction(
            document_id=b9_precedent.id,
            reaction=UserReactionType.PRECEDENT,
            notes="BGH XII ZB 601/15 — maßgeblich für §1671-Auslegung",
        ),
        UserReaction(document_id=b9_true.id, reaction=UserReactionType.TRUE),
    ]
)
db.commit()


# ═══════════════════════════════════════════════════════════════════════════
# BUNDLE 10 — SYNTHETIC: loose doc with no batch (synthetic bundle key=loose-N)
#   Tests: ingest_batch_id=None path; loose_docs_condition filter; MANUAL icon
# ═══════════════════════════════════════════════════════════════════════════
b10_doc = make_doc(
    "Einzel-Schriftstück — kein Batch (lose eingelegt)",
    batch=None,
    role=DocumentRole.STANDALONE,
    originator=OriginatorType.OWN,
    sender="eigene-kanzlei@sanctuary.de",
    case_id="_TRIAGE",
    needs_review=True,
    review_reasons=["missing_case_id"],
    significance=SignificanceTier.INFORMATIONAL,
    doc_type=DocumentType.CORRESPONDENCE,
    ai_summary_status="generated",
)
db.commit()


# ═══════════════════════════════════════════════════════════════════════════
# BUNDLE 11 — COMPLETED: already left triage; must NOT appear in feed
#   Tests: IngestBatch.status=COMPLETED excluded by get_triage_bundles query
# ═══════════════════════════════════════════════════════════════════════════
b11 = make_batch(
    IngestBatchSourceType.EMAIL,
    "Bereits bestätigtes Bundle — darf NICHT in Triage erscheinen",
    sender_email="done@kanzlei.de",
    case_id="ADV-100-X",
    status=IngestBatchStatus.COMPLETED,
    days_ago=10,
)

make_doc(
    "Bestätigter Schriftsatz (nicht in Triage sichtbar)",
    batch=b11,
    role=DocumentRole.STANDALONE,
    originator=OriginatorType.COURT,
    sender="done@kanzlei.de",
    case_id="ADV-100-X",
    needs_review=False,
    review_reasons=[],
    significance=SignificanceTier.INFORMATIONAL,
    doc_type=DocumentType.CORRESPONDENCE,
    ai_summary_status="generated",
)
db.commit()


# ── Summary ─────────────────────────────────────────────────────────────────
from app.models.database import ActionItem as _AI
from app.models.database import Document as _Doc
from app.models.database import IngestBatch as _Batch

total_docs = db.query(_Doc).filter(_Doc.case_id.in_(SEED_CASE_IDS)).count()
total_batches = db.query(_Batch).count()
total_actions = db.query(_AI).filter(_AI.case_id.in_(SEED_CASE_IDS)).count()
needs_review = (
    db.query(_Doc)
    .filter(_Doc.case_id.in_(SEED_CASE_IDS), _Doc.needs_review.is_(True))
    .count()
)

print("Triage seed complete:")
print(f"  Cases:          {len(SEED_CASE_IDS)}")
print("  Proceedings:    2  (AG Hamburg 003 F 426/25 / LG Berlin 14 O 123/25)")
print(f"  Batches total:  {total_batches}")
print(f"  Documents:      {total_docs}  ({needs_review} need review)")
print(f"  Action items:   {total_actions}")
print()
print("Bundles in triage feed (expected 10, bundle 11 excluded):")
print(
    "  1  CLEAN         — 2 docs confirmed ✓, Confirm bundle → active, proceeding chip"
)
print("  2  PARTIAL       — 1 confirmed ✓, 2 still pending; CTA disabled")
print("  3  FRESH         — 4 docs, all 4 pipeline states (pending/AI/ready/failed)")
print("  4  PROOF_PILL    — [proof] badge on Anlage S1; ActionItem under _TRIAGE")
print("  5  MULTI_ROOT    — 2 cover-letter subtrees → Bundle A / Bundle B")
print("  6  DEEP_NEST     — depth-2 grandchild (L-connector at px-12)")
print("  7  CRITICAL_SCAN — CRITICAL ruling, ai=failed, floats to top by urgency")
print("  8  LOW_CONF      — UNKNOWN originator, all-low confidence, all fields open")
print("  9  REACTIONS     — LIES / NEEDS_PROOF / PRECEDENT / TRUE pre-seeded")
print(" 10  SYNTHETIC     — loose doc, no batch (loose-N key, MANUAL icon)")
print(" 11  COMPLETED     — EXCLUDED from feed (status=COMPLETED)")

db.close()
