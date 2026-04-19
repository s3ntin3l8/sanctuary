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

# FK-safe deletion order: children before parents.
_seed_doc_ids = db.query(Document.id).filter(Document.case_id.in_(SEED_CASE_IDS))
db.query(DocumentRelationship).filter(
    DocumentRelationship.from_document_id.in_(_seed_doc_ids)
).delete(synchronize_session=False)
db.query(UserReaction).filter(UserReaction.document_id.in_(_seed_doc_ids)).delete(
    synchronize_session=False
)
db.query(ActionItem).filter(ActionItem.case_id.in_(SEED_CASE_IDS)).delete(
    synchronize_session=False
)
db.query(Document).filter(Document.case_id.in_(SEED_CASE_IDS)).delete(
    synchronize_session=False
)
db.query(IngestBatch).filter(IngestBatch.case_id.in_(SEED_CASE_IDS)).delete(
    synchronize_session=False
)
# Orphaned batches (case_id=None) from previous runs — identified by subject prefix.
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
    # German Legal Content Templates
    COURT_TEMPLATE = f"""# {title}
**AKTENZEICHEN:** 003 F 426/25
**DATUM:** {datetime.now().strftime("%d.%m.%Y")}

## BESCHLUSS
In der Familiensache **Vane ./. Vane** wegen elterlicher Sorge wird gemäß § 1671 BGB folgendes beschlossen:

1. Die elterliche Sorge für das gemeinsame Kind **Lukas Vane** wird der Kindesmutter zur alleinigen Ausübung übertragen.
2. Die Kosten des Verfahrens werden gegeneinander aufgehoben.
3. Der Verfahrenswert wird auf 3.000,00 EUR festgesetzt.

### GRÜNDE
Die Parteien sind die gemeinsam sorgeberechtigten Eltern des betroffenen Kindes. Die Trennung erfolgte im Januar 2025. Seither lebt das Kind im Haushalt der Mutter. Ein Einvernehmen über die Ausübung der Sorge konnte nicht erzielt werden...

> "Eine Aufhebung der gemeinsamen Sorge ist erforderlich, da die Kommunikation der Eltern nachhaltig gestört ist."
"""

    LAWYER_TEMPLATE = f"""# {title}
**AN DIE GEGENSEITE**
**UNSER ZEICHEN:** 8124/25 HB

## KLAGEERWIDERUNG
In dem Rechtsstreit **Meridian Holdings ./. Stadtplanung Berlin** nehmen wir Bezug auf die Klageschrift vom 10.02.2026.

Es wird beantragt:
**DIE KLAGE ABZUWEISEN.**

### BEGRÜNDUNG
Die Klägerin verkennt die Rechtslage hinsichtlich der Erschließungspflicht nach § 123 BauGB. Die behaupteten Mängel im Bebauungsplan liegen nicht vor. Insbesondere wurde das Gutachten der Gegenseite (Anlage K1) methodisch fehlerhaft erstellt...

Streitwert: **120.000,00 EUR**
"""

    REPORT_TEMPLATE = f"""# {title}
**JUGENDAMT HAMBURG-MITTE**
**FACHBEREICH:** Sozialer Dienst

## BERICHT FÜR DAS FAMILIENGERICHT
Gemäß § 50 SGB VIII nimmt das Jugendamt zur Frage der elterlichen Sorge Stellung.

Das Kind Lukas macht einen aufgeweckten und altersgemäß entwickelten Eindruck. Die Bindung zu beiden Elternteilen ist als stabil einzustufen. Dennoch zeigen sich im Gespräch mit den Eltern erhebliche Defizite in der Kooperationsfähigkeit...

**EMPFEHLUNG:**
Es wird empfohlen, die Entscheidung über die Sorge bis zum Abschluss der Erziehungsberatung auszusetzen.
"""

    if originator == OriginatorType.COURT:
        return COURT_TEMPLATE
    elif originator == OriginatorType.OPPOSING:
        return LAWYER_TEMPLATE
    elif originator == OriginatorType.THIRD_PARTY:
        return REPORT_TEMPLATE
    else:
        return f"# {title}\n\nStandard-Dokumententext für {sender}."


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
    attributed_originator="court",
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
# Wire the confirmed ADV-024-A docs into the proceeding so Phase 8 graph picks them up
b1_cover.proceeding_id = proc_a.id
b1_ruling.proceeding_id = proc_a.id
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


# ═══════════════════════════════════════════════════════════════════════════
# PHASE 8 — ADV-024-A correspondence graph (10 docs, 4 swim lanes)
#   Tests: proceeding-scoped graph, relay bundle, ghost node, thread_open,
#          cost_delta, key_passages, multi-party actors, typed relationships.
# ═══════════════════════════════════════════════════════════════════════════


# Phase 8 docs are confirmed into the proceeding (not triage-state), so they
# skip the make_doc helper (which enforces needs_review semantics).
def _p8_doc(**kwargs):
    """Phase 8 doc factory — sensible defaults for confirmed ADV-024-A docs."""
    defaults = {
        "case_id": "ADV-024-A",
        "proceeding_id": proc_a.id,
        "needs_review": False,
        "review_reasons": [],
        "ingest_status": IngestStatus.COMPLETED,
        "ai_summary_status": "generated",
        "role": DocumentRole.STANDALONE,
        "court_relay": False,
        "thread_open": False,
        "extraction_confidence": {
            "sender": "high",
            "date": "high",
            "case_id": "high",
            "originator": "high",
        },
    }
    defaults.update(kwargs)
    # Render canned content from title + originator if caller didn't supply it
    if "content" not in defaults:
        defaults["content"] = _content(
            defaults["title"],
            defaults.get("originator_type", OriginatorType.UNKNOWN),
            defaults.get("sender"),
        )
    doc = Document(**defaults)
    db.add(doc)
    db.flush()
    return doc


# 1. Klage (YOU → Complaint) — anchors the case
p8_klage = _p8_doc(
    title="Klage",
    document_type=DocumentType.MOTION,
    originator_type=OriginatorType.OWN,
    attributed_originator=None,
    sender="kanzlei@sanctuary-counsel.de",
    received_date=datetime(2025, 11, 15),
    created_at=datetime(2025, 11, 15),
    significance_tier=SignificanceTier.CRITICAL,
    ai_summary=[
        {
            "kind": "legal",
            "text": "Klage eingereicht gegen Beklagten wegen Unterhaltsrückständen i.H.v. 24.000 €.",
        },
        {
            "kind": "action",
            "text": "Gerichtskosten-Vorschuss von 747 € innerhalb von 2 Wochen einzuzahlen.",
        },
        {"kind": "finance", "text": "Streitwert: 24.000 €; GKG-Vorschuss fällig."},
    ],
    key_passages=[
        {
            "text": "Der Beklagte schuldet gemäß § 1601 BGB rückständigen Unterhalt für den Zeitraum Januar bis Oktober 2025.",
            "kind": "holding",
            "page": 2,
        },
        {
            "text": "Antrag auf Prozesskostenhilfe wird gestellt.",
            "kind": "neutral",
            "page": 4,
        },
    ],
    cost_delta=None,
)

# 2. Eingangsbestätigung (COURT → acknowledgement)
p8_eingang = _p8_doc(
    title="Eingangsbestätigung",
    document_type=DocumentType.CORRESPONDENCE,
    originator_type=OriginatorType.COURT,
    attributed_originator=None,
    sender="geschaeftsstelle@ag-hamburg.de",
    received_date=datetime(2025, 11, 20),
    created_at=datetime(2025, 11, 20),
    significance_tier=SignificanceTier.ADMINISTRATIVE,
    ai_summary=[
        {
            "kind": "legal",
            "text": "Eingang der Klage beim AG Hamburg bestätigt, Az. 003 F 426/25 zugeteilt.",
        }
    ],
    key_passages=[],
    cost_delta=None,
)

# 3. Kostenvorschussanforderung (COURT → cost request)
p8_kostenvorschuss = _p8_doc(
    title="Kostenvorschussanforderung",
    document_type=DocumentType.CORRESPONDENCE,
    originator_type=OriginatorType.COURT,
    attributed_originator=None,
    sender="geschaeftsstelle@ag-hamburg.de",
    received_date=datetime(2025, 11, 25),
    created_at=datetime(2025, 11, 25),
    significance_tier=SignificanceTier.SIGNIFICANT,
    ai_summary=[
        {
            "kind": "action",
            "text": "GKG-Vorschuss i.H.v. 747 € bis 09.12.2025 einzuzahlen, sonst Klagerücknahmefiktion.",
        },
        {"kind": "finance", "text": "747 € fällig innerhalb von 14 Tagen."},
    ],
    key_passages=[
        {
            "text": "Bei Nichteinzahlung gilt die Klage gemäß § 12 Abs. 3 GKG als zurückgenommen.",
            "kind": "deadline",
            "page": 1,
        }
    ],
    cost_delta={"amount": 747, "direction": "debit", "description": "GKG Vorschuss"},
)

# 4. Einzahlung GKG (YOU → payment)
p8_einzahlung = _p8_doc(
    title="Einzahlung GKG",
    document_type=DocumentType.CORRESPONDENCE,
    originator_type=OriginatorType.OWN,
    attributed_originator=None,
    sender="kanzlei@sanctuary-counsel.de",
    received_date=datetime(2025, 12, 5),
    created_at=datetime(2025, 12, 5),
    significance_tier=SignificanceTier.ADMINISTRATIVE,
    ai_summary=[
        {
            "kind": "finance",
            "text": "GKG-Vorschuss von 747 € eingezahlt. Quittung liegt vor.",
        }
    ],
    key_passages=[],
    cost_delta={
        "amount": 747,
        "direction": "debit",
        "description": "GKG Vorschuss eingezahlt",
    },
)

# 5. Beschluss Zustellung (COURT → ruling)
p8_zustellung = _p8_doc(
    title="Beschluss Zustellung",
    document_type=DocumentType.RULING,
    originator_type=OriginatorType.COURT,
    attributed_originator=None,
    sender="geschaeftsstelle@ag-hamburg.de",
    received_date=datetime(2025, 12, 20),
    created_at=datetime(2025, 12, 20),
    significance_tier=SignificanceTier.SIGNIFICANT,
    ai_summary=[
        {
            "kind": "legal",
            "text": "Gericht ordnet Zustellung der Klage an Beklagten an.",
        },
        {"kind": "action", "text": "Beklagter hat 4 Wochen Zeit zur Klageerwiderung."},
    ],
    key_passages=[
        {
            "text": "Die Klage wird dem Beklagten förmlich zugestellt. Frist zur Klageerwiderung: 4 Wochen.",
            "kind": "deadline",
            "page": 1,
        }
    ],
    cost_delta=None,
)

# 6. Beglaubigung (COURT relay bundle → cover letter)
p8_beglaubigung = _p8_doc(
    title="Beglaubigung",
    document_type=DocumentType.RELAY,
    originator_type=OriginatorType.COURT,
    attributed_originator=None,
    sender="geschaeftsstelle@ag-hamburg.de",
    received_date=datetime(2026, 1, 20),
    created_at=datetime(2026, 1, 20),
    significance_tier=SignificanceTier.SIGNIFICANT,
    court_relay=True,
    role=DocumentRole.COVER_LETTER,
    ai_summary=[
        {
            "kind": "legal",
            "text": "Gerichtliche Weiterleitung von Klageerwiderung und Jugendamtsbericht.",
        }
    ],
    key_passages=[],
    cost_delta=None,
)

# 7. Klageerwiderung (OPPOSING via court relay — child of Beglaubigung)
p8_klageerwiderung = _p8_doc(
    title="Klageerwiderung",
    document_type=DocumentType.STATEMENT,
    originator_type=OriginatorType.COURT,  # routed via court
    attributed_originator="opposing",  # true sender
    sender="geschaeftsstelle@ag-hamburg.de",
    received_date=datetime(2026, 1, 20),
    created_at=datetime(2026, 1, 20),
    significance_tier=SignificanceTier.SIGNIFICANT,
    role=DocumentRole.ENCLOSURE,
    ai_summary=[
        {
            "kind": "legal",
            "text": "Beklagter bestreitet Unterhaltsrückstände und behauptet verringerte Leistungsfähigkeit.",
        },
        {
            "kind": "action",
            "text": "Stellungnahme zu den bestrittenen Fakten erforderlich.",
        },
        {
            "kind": "finance",
            "text": "Beklagter beansprucht Kostenerstattung i.H.v. 1.240 € (§ 91 ZPO) für Anwaltskosten.",
        },
    ],
    key_passages=[
        {
            "text": "Die Klageforderung wird in vollem Umfang bestritten. Der Beklagte war in dem genannten Zeitraum nicht leistungsfähig.",
            "kind": "holding",
            "page": 1,
        },
        {
            "text": "Antrag: Die Klage wird abgewiesen. Kosten trägt die Klägerin.",
            "kind": "holding",
            "page": 3,
        },
    ],
    cost_delta={
        "amount": 1240,
        "direction": "opposing_claim",
        "description": "§ 91 ZPO Anwaltskosten Beklagter",
    },
)

# 8. Jugendamtsbericht (THIRD_PARTY via court relay — child of Beglaubigung, thread open)
p8_jugendamt = _p8_doc(
    title="Jugendamtsbericht",
    document_type=DocumentType.REPORT,
    originator_type=OriginatorType.COURT,
    attributed_originator="third_party",
    sender="geschaeftsstelle@ag-hamburg.de",
    received_date=datetime(2026, 1, 20),
    created_at=datetime(2026, 1, 20),
    significance_tier=SignificanceTier.SIGNIFICANT,
    role=DocumentRole.ENCLOSURE,
    thread_open=True,  # amber glow
    ai_summary=[
        {
            "kind": "legal",
            "text": "Jugendamt bestätigt Betreuungssituation, aber keine Angaben zur Einkommenssituation.",
        },
        {
            "kind": "action",
            "text": "Rückfrage beim Jugendamt zur Einkommenssituation des Beklagten ausstehend.",
        },
    ],
    key_passages=[
        {
            "text": "Das Jugendamt hat keine Kenntnis von der aktuellen Einkommenssituation des Beklagten.",
            "kind": "neutral",
            "page": 2,
        }
    ],
    cost_delta=None,
)

# Wire relay children to the Beglaubigung cover letter (after flush for IDs)
p8_klageerwiderung.parent_id = p8_beglaubigung.id
p8_jugendamt.parent_id = p8_beglaubigung.id
db.flush()

# 9. Beschluss PKH (COURT → ruling, critical)
p8_pkh = _p8_doc(
    title="Beschluss PKH gewährt",
    document_type=DocumentType.RULING,
    originator_type=OriginatorType.COURT,
    attributed_originator=None,
    sender="geschaeftsstelle@ag-hamburg.de",
    received_date=datetime(2026, 2, 4),
    created_at=datetime(2026, 2, 4),
    significance_tier=SignificanceTier.CRITICAL,
    ai_summary=[
        {
            "kind": "legal",
            "text": "Prozesskostenhilfe bewilligt. Klägerin muss keinen weiteren Vorschuss leisten.",
        },
        {
            "kind": "action",
            "text": "Stellungnahme zur Klageerwiderung bis 30.04.2026 einzureichen.",
        },
        {
            "kind": "finance",
            "text": "PKH reduziert direktes Kostenrisiko. Erstattungspflicht bleibt bei Unterliegen.",
        },
    ],
    key_passages=[
        {
            "text": "Der Klägerin wird Prozesskostenhilfe ohne Ratenzahlung bewilligt.",
            "kind": "holding",
            "page": 1,
        },
        {
            "text": "Stellungnahme zur Klageerwiderung ist bis zum 30. April 2026 beim Gericht einzureichen.",
            "kind": "deadline",
            "page": 2,
        },
    ],
    cost_delta={
        "amount": 450,
        "direction": "credit",
        "description": "PKH bewilligt — Vorschuss entfällt",
    },
)

# 10. Stellungnahme (YOU → ghost/pending, no received_date)
p8_stellungnahme = _p8_doc(
    title="Stellungnahme",
    document_type=DocumentType.STATEMENT,
    originator_type=OriginatorType.OWN,
    attributed_originator=None,
    sender="kanzlei@sanctuary-counsel.de",
    received_date=None,  # ghost — not yet filed
    created_at=datetime(2026, 4, 19),
    significance_tier=SignificanceTier.CRITICAL,
    thread_open=True,
    ai_summary_status="pending",
    ai_summary=[
        {
            "kind": "action",
            "text": "Stellungnahme zur Klageerwiderung — noch nicht eingereicht. Frist: 30.04.2026.",
        }
    ],
    key_passages=[],
    cost_delta=None,
)
db.flush()

# ── DocumentRelationships (8) ──────────────────────────────────────────────
db.add_all(
    [
        # PKH ruling opens the reply thread that Stellungnahme will close
        DocumentRelationship(
            from_document_id=p8_stellungnahme.id,
            to_document_id=p8_pkh.id,
            relationship_type=RelationshipType.REPLIES_TO,
            confidence=RelationshipConfidence.AI_DETECTED,
        ),
        # Klageerwiderung is the answer to the Beschluss Zustellung
        DocumentRelationship(
            from_document_id=p8_klageerwiderung.id,
            to_document_id=p8_zustellung.id,
            relationship_type=RelationshipType.REPLIES_TO,
            confidence=RelationshipConfidence.AI_DETECTED,
        ),
        # Klageerwiderung references the original Klage
        DocumentRelationship(
            from_document_id=p8_klageerwiderung.id,
            to_document_id=p8_klage.id,
            relationship_type=RelationshipType.REFERENCES,
            confidence=RelationshipConfidence.AI_DETECTED,
        ),
        # Jugendamtsbericht references the Klageerwiderung it accompanies
        DocumentRelationship(
            from_document_id=p8_jugendamt.id,
            to_document_id=p8_klageerwiderung.id,
            relationship_type=RelationshipType.REFERENCES,
            confidence=RelationshipConfidence.AI_DETECTED,
        ),
        # Einzahlung is the proof-of-payment for the cost request
        DocumentRelationship(
            from_document_id=p8_einzahlung.id,
            to_document_id=p8_kostenvorschuss.id,
            relationship_type=RelationshipType.ATTACHES_AS_PROOF,
            confidence=RelationshipConfidence.USER_CONFIRMED,
        ),
        # Stellungnahme will reference Klageerwiderung it rebuts
        DocumentRelationship(
            from_document_id=p8_stellungnahme.id,
            to_document_id=p8_klageerwiderung.id,
            relationship_type=RelationshipType.REFERENCES,
            confidence=RelationshipConfidence.AI_DETECTED,
        ),
        # PKH ruling supersedes the original cost request (no further advances)
        DocumentRelationship(
            from_document_id=p8_pkh.id,
            to_document_id=p8_kostenvorschuss.id,
            relationship_type=RelationshipType.SUPERSEDES,
            confidence=RelationshipConfidence.AI_DETECTED,
        ),
        # Eingangsbestätigung references the Klage it acknowledges
        DocumentRelationship(
            from_document_id=p8_eingang.id,
            to_document_id=p8_klage.id,
            relationship_type=RelationshipType.REFERENCES,
            confidence=RelationshipConfidence.AI_DETECTED,
        ),
    ]
)

# ── Case ai_brief, parties, total_cost_exposure ────────────────────────────
case_a.ai_brief = {
    "schema_version": 1,
    "status_line": (
        "PKH bewilligt. Klageerwiderung des Beklagten bestreitet Leistungsfähigkeit. "
        "Stellungnahme bis 30.04.2026 fällig. Jugendamtsbericht lückenhaft — "
        "Rückfrage offen."
    ),
    "key_risks": [
        {
            "id": "r1",
            "severity": "critical",
            "label": "Fristversäumnis Stellungnahme",
            "sub": "30.04.2026 — 11 Tage",
        },
        {
            "id": "r2",
            "severity": "near",
            "label": "Einkommensbehauptung unbelegt",
            "sub": "Beklagter bestreitet Leistungsfähigkeit ohne Nachweis",
        },
        {
            "id": "r3",
            "severity": "near",
            "label": "Jugendamt-Lücke",
            "sub": "Keine Einkommensauskunft erhalten",
        },
    ],
    "open_threads": [
        {
            "thread": "Jugendamtsbericht",
            "description": "Rückfrage zur Einkommenssituation ausstehend",
        },
        {"thread": "Stellungnahme", "description": "Entwurf noch nicht begonnen"},
    ],
    "recent_development": (
        "PKH bewilligt am 04.02.2026 — Klageerwiderung des Beklagten "
        "bestreitet sämtliche Forderungen."
    ),
}
case_a.ai_brief_updated_at = datetime(2026, 2, 4)
case_a.parties = [
    {"key": "klaegerin", "color": "own", "label": "Klägerin", "name": "Björn Hansen"},
    {
        "key": "beklagter",
        "color": "opposing",
        "label": "Beklagter",
        "name": "M. Müller",
    },
    {"key": "gericht", "color": "court", "label": "Gericht", "name": "AG Hamburg"},
    {
        "key": "jugendamt",
        "color": "third",
        "label": "Dritter",
        "name": "Jugendamt Hamburg-Nord",
    },
]
case_a.total_cost_exposure = 16900000  # cents — 169.000 €

# ── ActionItems ────────────────────────────────────────────────────────────
db.add_all(
    [
        ActionItem(
            case_id="ADV-024-A",
            proceeding_id=proc_a.id,
            source_document_id=p8_pkh.id,
            title="Stellungnahme zur Klageerwiderung einreichen",
            description=(
                "Fristsetzung durch Gericht: 30.04.2026. PKH-Beschluss vom 04.02.2026."
            ),
            due_date=datetime(2026, 4, 30),
            action_type=ActionItemType.DEADLINE,
            status=ActionItemStatus.OPEN,
        ),
        ActionItem(
            case_id="ADV-024-A",
            proceeding_id=proc_a.id,
            source_document_id=None,
            title="Verhandlungstermin (voraussichtlich)",
            description=(
                "Typisches Tempo AG Hamburg: ca. 6 Monate nach Klageerwiderung."
            ),
            due_date=datetime(2026, 6, 15),
            action_type=ActionItemType.COURT_DATE,
            status=ActionItemStatus.OPEN,
        ),
    ]
)

# ── UserReactions (3) ──────────────────────────────────────────────────────
db.add_all(
    [
        UserReaction(
            document_id=p8_klageerwiderung.id,
            reaction=UserReactionType.LIES,
            notes="Leistungsfähigkeit bestritten — widerspricht Kontoauszügen",
        ),
        UserReaction(
            document_id=p8_jugendamt.id,
            reaction=UserReactionType.NEEDS_PROOF,
            notes="Einkommensauskunft fehlt — Rückfrage senden",
        ),
        UserReaction(
            document_id=p8_pkh.id,
            reaction=UserReactionType.TRUE,
            notes="PKH rechtskräftig bewilligt",
        ),
    ]
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
print()
print("Phase 8 graph — ADV-024-A (proceeding 003 F 426/25):")
print("  10 documents across 4 swim lanes (court / own / opposing / third_party)")
print("  1 relay bundle (Beglaubigung → Klageerwiderung + Jugendamtsbericht)")
print("  1 ghost node (Stellungnahme, received_date=None)")
print("  2 thread_open docs, 3 user reactions, 2 action items")
print("  8 typed DocumentRelationships (REPLIES_TO / REFERENCES /")
print("    ATTACHES_AS_PROOF / SUPERSEDES)")

db.close()
