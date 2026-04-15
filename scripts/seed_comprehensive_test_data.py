"""Seed script: generates comprehensive test documents covering ALL variations
for UI/functionality testing including all review reasons, AI summary states,
originator types, parent-child relationships, cost categories, etc."""

import os
import random
import sys
from datetime import UTC, datetime, timedelta
from pathlib import Path

# Add app to path
sys.path.append(str(Path(__file__).resolve().parent.parent))

os.environ.setdefault("SQLALCHEMY_DATABASE_URL", "sqlite:///./data/sanctuary.db")

from app.config import SessionLocal, engine
from app.models.database import (
    Base,
    Case,
    CaseStatus,
    CostCategory,
    CostStatus,
    Deadline,
    Document,
    Hearing,
    Jurisdiction,
    LegalCost,
    OriginatorType,
)
from app.services.normalization import normalize_hm

Base.metadata.create_all(bind=engine, checkfirst=True)
db = SessionLocal()

# ── Test Cases with varied statuses and jurisdictions ─────────────────────────
TEST_CASES = [
    {
        "id": "TEST-IN-001",
        "title": "Test Case: Intake Status - DE Jurisdiction",
        "court_id": "2025-TEST-IN-001",
        "status": CaseStatus.INTAKE,
        "jurisdiction": Jurisdiction.DE,
    },
    {
        "id": "TEST-DS-002",
        "title": "Test Case: Discovery - US Jurisdiction",
        "court_id": "2024-CV-TEST-002",
        "status": CaseStatus.DISCOVERY,
        "jurisdiction": Jurisdiction.US,
    },
    {
        "id": "TEST-PT-003",
        "title": "Test Case: Pre-Trial - UK Jurisdiction",
        "court_id": "2024-UK-TEST-003",
        "status": CaseStatus.PRE_TRIAL,
        "jurisdiction": Jurisdiction.UK,
    },
    {
        "id": "TEST-TR-004",
        "title": "Test Case: Trial - OTHER Jurisdiction",
        "court_id": "2025-INT-TEST-004",
        "status": CaseStatus.TRIAL,
        "jurisdiction": Jurisdiction.OTHER,
    },
    {
        "id": "TEST-PO-005",
        "title": "Test Case: Post-Trial - DE Jurisdiction",
        "court_id": "2023-AP-TEST-005",
        "status": CaseStatus.POST_TRIAL,
        "jurisdiction": Jurisdiction.DE,
    },
    {
        "id": "TEST-CL-006",
        "title": "Test Case: Closed - US Jurisdiction",
        "court_id": "2022-CLOSED-006",
        "status": CaseStatus.CLOSED,
        "jurisdiction": Jurisdiction.US,
    },
]

for seed in TEST_CASES:
    if not db.get(Case, seed["id"]):
        db.add(Case(**seed))
db.commit()

# ── Content Templates ─────────────────────────────────────────────────────────
SENDERS = {
    OriginatorType.COURT: [
        "Clerk's Office <clerk@testcourt.de>",
        "Judge Test <judge@testcourt.de>",
        "Court Administrator <admin@testcourt.de>",
    ],
    OriginatorType.OPPOSING: [
        "Opposing Counsel <opposing@testlaw.de>",
        "External Party <external@test.de>",
    ],
    OriginatorType.OWN: [
        "Internal Counsel <internal@test.de>",
        "Legal Team <legal@test.de>",
    ],
    OriginatorType.UNKNOWN: [
        "Unknown Sender <unknown@test.de>",
        "Automated System <auto@test.de>",
    ],
}

DOCUMENT_TEMPLATES = [
    {
        "title": "Court Order - Test Motion",
        "originator": OriginatorType.COURT,
        "content": "This is a test court order document for testing purposes. Order issued on test date.",
    },
    {
        "title": "Settlement Offer from Opposing",
        "originator": OriginatorType.OPPOSING,
        "content": "This is a test settlement offer from opposing party. Amount: EUR 50,000.",
    },
    {
        "title": "Internal Strategy Memo",
        "originator": OriginatorType.OWN,
        "content": "Internal strategy document for test case. Contains confidential legal analysis.",
    },
    {
        "title": "Automated System Notification",
        "originator": OriginatorType.UNKNOWN,
        "content": "Automated notification from system. No human sender identified.",
    },
]


# ── Helper: Create a document with specific variations ───────────────────────
def create_test_doc(
    case_id: str,
    title: str,
    content: str,
    originator: OriginatorType,
    sender: str,
    received_date: datetime,
    needs_review: bool = False,
    review_reasons: list = None,
    parent_id: int = None,
    ai_summary_status: str = "pending",
    extraction_confidence: dict = None,
    case_id_value: str = None,
):
    doc = Document(
        title=title,
        content=normalize_hm(content),
        case_id=case_id_value or (case_id if random.random() > 0.1 else None),
        originator_type=originator,
        sender=sender if random.random() > 0.1 else None,
        received_date=received_date if random.random() > 0.1 else None,
        needs_review=needs_review,
        review_reasons=review_reasons or [],
        parent_id=parent_id,
        ai_summary_status=ai_summary_status,
        extraction_confidence=extraction_confidence,
    )
    return doc


# ── Generate Test Documents ───────────────────────────────────────────────────
random.seed(123)  # Different seed for reproducibility
now = datetime.now(UTC).replace(second=0, microsecond=0)

# Track parent doc IDs for nesting
parent_docs_by_case = {case["id"]: [] for case in TEST_CASES}

for case_seed in TEST_CASES:
    case_id = case_seed["id"]
    case_docs = []

    # Group 1: Documents with various review_reasons (missing_*) ──────────────
    review_test_docs = [
        (True, ["missing_case_id"], "Case ID Missing"),
        (True, ["missing_originator"], "Originator Missing"),
        (True, ["missing_sender"], "Sender Missing"),
        (True, ["missing_received_date"], "Received Date Missing"),
        (True, ["missing_parent"], "Parent Missing"),
        (True, ["missing_case_id", "missing_sender"], "Multiple Missing"),
    ]

    for needs_rev, reasons, title_suffix in review_test_docs:
        template = random.choice(DOCUMENT_TEMPLATES)
        originator = template["originator"]
        sender = random.choice(SENDERS.get(originator, ["test@test.de"]))

        doc = create_test_doc(
            case_id=case_id,
            title=f"Review Test: {title_suffix}",
            content=template["content"],
            originator=originator,
            sender=sender,
            received_date=now - timedelta(days=random.randint(1, 30)),
            needs_review=needs_rev,
            review_reasons=reasons,
            case_id_value=None,  # Force missing for test
        )
        # Override to ensure exact test scenario
        if "missing_case_id" in reasons:
            doc.case_id = None
        if "missing_sender" in reasons:
            doc.sender = None
        if "missing_received_date" in reasons:
            doc.received_date = None
        if "missing_originator" in reasons:
            doc.originator_type = OriginatorType.UNKNOWN

        db.add(doc)
        db.flush()
        case_docs.append(doc)
        # Some become parents
        if random.random() < 0.5:
            parent_docs_by_case[case_id].append(doc.id)

    db.commit()

    # Group 2: All originator types ─────────────────────────────────────────────
    for ot in OriginatorType:
        template = random.choice(DOCUMENT_TEMPLATES)
        sender = random.choice(SENDERS.get(ot, ["unknown@test.de"]))

        doc = create_test_doc(
            case_id=case_id,
            title=f"Originator Test: {ot.value}",
            content=template["content"],
            originator=ot,
            sender=sender,
            received_date=now - timedelta(days=random.randint(1, 30)),
            needs_review=False,
            ai_summary_status="generated",
        )
        db.add(doc)
        db.flush()
        case_docs.append(doc)
        if random.random() < 0.4:
            parent_docs_by_case[case_id].append(doc.id)

    db.commit()

    # Group 3: All AI summary statuses ─────────────────────────────────────────
    ai_statuses = ["pending", "generated", "failed", "stale", "approved"]
    for status in ai_statuses:
        template = random.choice(DOCUMENT_TEMPLATES)
        originator = template["originator"]
        sender = random.choice(SENDERS.get(originator, ["test@test.de"]))

        doc = create_test_doc(
            case_id=case_id,
            title=f"AI Status Test: {status}",
            content=template["content"],
            originator=originator,
            sender=sender,
            received_date=now - timedelta(days=random.randint(1, 30)),
            needs_review=False,
            ai_summary_status=status,
        )
        if status == "approved":
            doc.ai_summary_approved_at = now - timedelta(days=random.randint(1, 10))
            doc.ai_summary = {
                "legal_significance": "Test approved summary",
                "required_action": "No action required",
                "financial_impact": "None",
            }
        elif status == "generated":
            doc.ai_summary = {
                "legal_significance": "Test generated summary",
                "required_action": "Review recommended",
                "financial_impact": "Low",
            }

        db.add(doc)
        db.flush()
        case_docs.append(doc)
        if random.random() < 0.4:
            parent_docs_by_case[case_id].append(doc.id)

    db.commit()

    # Group 4: Extraction confidence variations ─────────────────────────────────
    confidence_tests = [
        ({"sender": "high", "date": "high", "case_id": "high"}, "High Confidence"),
        ({"sender": "medium", "date": "high", "case_id": "low"}, "Mixed Confidence"),
        ({"sender": "low", "date": "low", "case_id": "medium"}, "Low Confidence"),
    ]

    for confidence, title_suffix in confidence_tests:
        template = random.choice(DOCUMENT_TEMPLATES)
        originator = template["originator"]
        sender = random.choice(SENDERS.get(originator, ["test@test.de"]))

        doc = create_test_doc(
            case_id=case_id,
            title=f"Confidence Test: {title_suffix}",
            content=template["content"],
            originator=originator,
            sender=sender,
            received_date=now - timedelta(days=random.randint(1, 30)),
            needs_review=False,
            extraction_confidence=confidence,
        )
        # Low/medium confidence triggers needs_review per compute_review_reasons
        if "low" in confidence.values() or "medium" in confidence.values():
            doc.needs_review = True

        db.add(doc)
        db.flush()
        case_docs.append(doc)
        if random.random() < 0.4:
            parent_docs_by_case[case_id].append(doc.id)

    db.commit()

    # Group 5: Parent-child nesting test ────────────────────────────────────────
    # Create some children for existing parents
    for parent_id in parent_docs_by_case[case_id][:3]:
        template = random.choice(DOCUMENT_TEMPLATES)
        originator = template["originator"]
        sender = random.choice(SENDERS.get(originator, ["test@test.de"]))

        child = create_test_doc(
            case_id=case_id,
            title=f"Child Document (parent: {parent_id})",
            content=f"Response to parent document.\n\n{template['content']}",
            originator=originator,
            sender=sender,
            received_date=now - timedelta(days=random.randint(1, 20)),
            needs_review=False,
            parent_id=parent_id,
            ai_summary_status="pending",
        )
        db.add(child)
        db.flush()
        case_docs.append(child)

    db.commit()

    # Group 6: title_too_short test ─────────────────────────────────────────────
    short_title_doc = create_test_doc(
        case_id=case_id,
        title="X",  # Very short title
        content="Document with very short title for testing.",
        originator=OriginatorType.OWN,
        sender="Internal <internal@test.de>",
        received_date=now - timedelta(days=5),
        needs_review=True,
        review_reasons=["title_too_short"],
    )
    db.add(short_title_doc)
    db.flush()
    case_docs.append(short_title_doc)

    db.commit()

    # Group 7: Documents without needs_review (clean) ──────────────────────────
    for i in range(5):
        template = random.choice(DOCUMENT_TEMPLATES)
        originator = template["originator"]
        sender = random.choice(SENDERS.get(originator, ["test@test.de"]))

        clean_doc = create_test_doc(
            case_id=case_id,
            title=f"Clean Document #{i + 1}",
            content=template["content"],
            originator=originator,
            sender=sender,
            received_date=now - timedelta(days=random.randint(1, 30)),
            needs_review=False,
            review_reasons=[],
            case_id_value=case_id,  # Ensure case_id is set
            extraction_confidence={"sender": "high", "date": "high", "case_id": "high"},
        )
        db.add(clean_doc)
        db.flush()

    db.commit()

    # ── Seed Deadlines (varied states) ───────────────────────────────────────
    deadline_configs = [
        ("Test Deadline - Past", -15, True),  # Past + completed
        ("Test Deadline - Past", -10, False),  # Past + incomplete
        ("Test Deadline - Future", 30, False),  # Future + incomplete
        ("Test Deadline - Future", 60, False),  # Future + incomplete
    ]

    for title, offset, completed in deadline_configs:
        deadline = Deadline(
            case_id=case_id,
            title=title,
            description=f"Test deadline: {title}",
            due_at=now + timedelta(days=offset),
            completed=completed,
        )
        db.add(deadline)

    db.commit()

    # ── Seed Hearings (past and future) ───────────────────────────────────────
    hearing_configs = [
        ("Test Hearing - Past", -20, "Test Court Room 1"),
        ("Test Hearing - Future", 45, "Test Court Room 2"),
    ]

    for title, offset, location in hearing_configs:
        hearing = Hearing(
            case_id=case_id,
            title=title,
            description=f"Test hearing: {title}",
            location=location,
            scheduled_for=now + timedelta(days=offset),
        )
        db.add(hearing)

    db.commit()

    # ── Seed Costs (all categories and statuses) ───────────────────────────────
    cost_configs = [
        (CostCategory.GERICHTSKOSTEN, CostStatus.OFFEN, "Court Fees Test", 500.00),
        (CostCategory.ANWALTSKOSTEN, CostStatus.BEZAHLT, "Attorney Fees Test", 2000.00),
        (
            CostCategory.ANWALTSKOSTEN_GEGNER,
            CostStatus.STRITTIG,
            "Opposing Counsel Test",
            1500.00,
        ),
        (
            CostCategory.SACHVERSTAENDIGER,
            CostStatus.ERSTATTET,
            "Expert Witness Test",
            800.00,
        ),
        (CostCategory.VORSCHUSS, CostStatus.OFFEN, "Advance Payment Test", 300.00),
        (
            CostCategory.VOLLSTRECKUNG,
            CostStatus.TEILWEISE,
            "Enforcement Costs Test",
            250.00,
        ),
        (CostCategory.AUSLAGEN, CostStatus.BEZAHLT, "Out-of-Pocket Test", 100.00),
        (CostCategory.SONSTIGES, CostStatus.OFFEN, "Other Costs Test", 75.00),
    ]

    for category, status, title, amount in cost_configs:
        cost = LegalCost(
            case_id=case_id,
            title=title,
            category=category,
            status=status,
            amount_net=amount,
            amount_gross=amount * 1.19
            if category != CostCategory.GERICHTSKOSTEN
            else amount,
            amount_paid=amount * 1.19
            if status in [CostStatus.BEZAHLT, CostStatus.ERSTATTET]
            else 0,
            amount_reimbursed=amount * 1.19 if status == CostStatus.ERSTATTET else 0,
            issued_at=now - timedelta(days=random.randint(5, 30)),
            due_at=now + timedelta(days=random.randint(-5, 30)),
            paid_at=now - timedelta(days=random.randint(1, 10))
            if status in [CostStatus.BEZAHLT, CostStatus.ERSTATTET]
            else None,
        )
        db.add(cost)

    db.commit()

# ── Summary ───────────────────────────────────────────────────────────────────
total_docs = db.query(Document).filter(Document.case_id.like("TEST-%")).count()
total_cases = db.query(Case).filter(Case.id.like("TEST-%")).count()
total_deadlines = db.query(Deadline).filter(Deadline.case_id.like("TEST-%")).count()
total_hearings = db.query(Hearing).filter(Hearing.case_id.like("TEST-%")).count()
total_costs = db.query(LegalCost).filter(LegalCost.case_id.like("TEST-%")).count()

# Breakdown by ai_summary_status
ai_status_breakdown = {}
for status in ["pending", "generated", "failed", "stale", "approved"]:
    count = (
        db.query(Document)
        .filter(Document.case_id.like("TEST-%"), Document.ai_summary_status == status)
        .count()
    )
    ai_status_breakdown[status] = count

# Breakdown by originator_type
originator_breakdown = {}
for ot in OriginatorType:
    count = (
        db.query(Document)
        .filter(Document.case_id.like("TEST-%"), Document.originator_type == ot)
        .count()
    )
    originator_breakdown[ot.value] = count

# Breakdown by needs_review
needs_review_true = (
    db.query(Document)
    .filter(Document.case_id.like("TEST-%"), Document.needs_review == True)
    .count()
)

print("Comprehensive test data seed complete:")
print(f"  Cases:      {total_cases}")
print(f"  Documents:  {total_docs}")
print(f"  Deadlines:  {total_deadlines}")
print(f"  Hearings:   {total_hearings}")
print(f"  Costs:      {total_costs}")
print(f"  Needs Review: {needs_review_true}")
print("\nAI Summary Status Breakdown:")
for status, count in ai_status_breakdown.items():
    print(f"  {status}: {count}")
print("\nOriginator Type Breakdown:")
for ot, count in originator_breakdown.items():
    print(f"  {ot}: {count}")

db.close()
