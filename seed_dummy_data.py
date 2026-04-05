"""Seed script: generates ~50 realistic dummy documents with varied content,
parent-child relationships, costs, deadlines, and hearings across 4 cases."""

import os
import random
from datetime import datetime, timedelta, timezone

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
    LegalCost,
    OriginatorType,
)
from app.services.normalization import normalize_hm

# Use checkfirst=True to avoid "table already exists" errors
Base.metadata.create_all(bind=engine, checkfirst=True)
db = SessionLocal()

# ── Seed Cases ──────────────────────────────────────────────────────────────
SEED_CASES = [
    {
        "id": "ADV-992-K",
        "title": "Vane vs. Vane: Divorce & Assets",
        "court_id": "2024-FL-DR-00992",
        "status": CaseStatus.DISCOVERY,
    },
    {
        "id": "ADV-804-M",
        "title": "Meridian Holdings v. City Planning Board",
        "court_id": "2023-CV-ADM-0804",
        "status": CaseStatus.PRE_TRIAL,
    },
    {
        "id": "ADV-331-P",
        "title": "Patel Estate: Probate & Distribution",
        "court_id": "2025-PR-EST-00331",
        "status": CaseStatus.INTAKE,
    },
    {
        "id": "ADV-550-R",
        "title": "Rothschild Corp. v. H&M Retail Group",
        "court_id": "2024-CV-COM-00550",
        "status": CaseStatus.DISCOVERY,
    },
]

for seed in SEED_CASES:
    if not db.get(Case, seed["id"]):
        db.add(Case(**seed))
db.commit()

case_ids = [c["id"] for c in SEED_CASES]

# ── Content Templates ───────────────────────────────────────────────────────
SENDERS = {
    OriginatorType.COURT: [
        "Clerk's Office <clerk@lg-berlin.de>",
        "Judge Richter <richter@olg-muenchen.de>",
        "Court Administrator <admin@ag-hamburg.de>",
        "Presiding Judge <presiding@bggh.de>",
    ],
    OriginatorType.OPPOSING: [
        "Dr. Mueller <mueller@kanzlei-feinde.de>",
        "Opposing Counsel <counsel@wright-law.com>",
        "Schmidt & Partners <info@schmidt-partner.de>",
        "Litigation Team <litigation@opposing-firm.de>",
    ],
    OriginatorType.OWN: [
        "Julian Vance <jvance@sanctuary-counsel.com>",
        "Sarah Chen <schen@sanctuary-counsel.com>",
        "Legal Assistant <assistant@sanctuary-counsel.com>",
    ],
}

DOCUMENT_TEMPLATES = [
    {
        "title": "Court Order — Motion to Compel Discovery",
        "originator": OriginatorType.COURT,
        "content": """ORDER ON MOTION TO COMPEL

This matter comes before the Court on Plaintiff's Motion to Compel Discovery.
The Court has reviewed the submissions of both parties and finds that the
Defendant has failed to produce documents responsive to Requests 1-7 within
the 30-day period prescribed by Rule 34.

IT IS HEREBY ORDERED that Defendant shall produce all responsive documents
within 14 days of this Order. Failure to comply may result in sanctions
including adverse inference instructions.

The hearing on the Motion for Summary Judgment is scheduled for March 15, 2026
at 9:00 AM in Courtroom 4B. Both parties must file their briefs no later than
14 days before the hearing date.

Court costs of EUR 425.00 are assessed against Defendant pursuant to
Gerichtskostengesetz (GKG) KV 1210.""",
    },
    {
        "title": "Opposing Counsel — Settlement Demand Letter",
        "originator": OriginatorType.OPPOSING,
        "content": """SETTLEMENT DEMAND

Dear Counsel,

We write on behalf of our client to formally present the following settlement
demand in the above-referenced matter.

Our client is willing to resolve this dispute for the sum of EUR 185,000.00,
payable within 30 days of execution of a full and final release. This demand
is inclusive of all claims, including attorney fees and costs.

Please note that this demand expires on February 28, 2026. If we do not
receive a counteroffer by that date, we will proceed with filing the
Amended Complaint and Motion for Preliminary Injunction.

Our client has also incurred H&M retail expenses totaling EUR 3,200.00
which are documented in Exhibit C and form part of the damages claim.

We look forward to your prompt response.""",
    },
    {
        "title": "Internal Memo — Case Strategy Review",
        "originator": OriginatorType.OWN,
        "content": """INTERNAL MEMORANDUM — PRIVILEGED & CONFIDENTIAL

TO: Case File
FROM: Julian Vance
RE: Strategy Review — Next Steps

After reviewing the opposing counsel's latest filing, I recommend the
following course of action:

1. File a Motion for Protective Order regarding the overly broad document
   requests (Requests 8-15).
2. Retain Dr. Weber as our expert witness on valuation matters.
3. Prepare deposition outlines for the three key witnesses identified
   during discovery.

The deadline for our response to their discovery requests is March 1, 2026.
We should schedule a client meeting for the week of February 17 to discuss
settlement parameters.

Budget estimate for expert witness: EUR 8,500.00 (Sachverständiger under JVEG).""",
    },
    {
        "title": "Notice of Hearing — Preliminary Injunction",
        "originator": OriginatorType.COURT,
        "content": """NOTICE OF HEARING

PLEASE TAKE NOTICE that a hearing on Plaintiff's Motion for Preliminary
Injunction has been scheduled as follows:

Date: April 2, 2026
Time: 10:30 AM
Location: Amtsgericht Berlin, Courtroom 2A
Judge: Hon. Richter

All parties must appear and be prepared to argue. Oral argument is limited
to 20 minutes per side.

The Court requires that all exhibits be filed at least 5 business days
prior to the hearing date.

Court filing fee of EUR 150.00 applies per GKG schedule.""",
    },
    {
        "title": "Discovery Response — Interrogatories",
        "originator": OriginatorType.OPPOSING,
        "content": """DEFENDANT'S RESPONSES TO PLAINTIFF'S FIRST SET OF INTERROGATORIES

Defendant responds to Plaintiff's Interrogatories as follows:

Interrogatory No. 1: Admitted.
Interrogatory No. 2: Objection. This interrogatory seeks information that
is protected by the attorney-client privilege.
Interrogatory No. 3: Defendant has no knowledge or information sufficient
to form a belief as to the truth of the matter.
Interrogatory No. 4: See attached Schedule A for complete response.

Pursuant to the applicable rules, Defendant reserves the right to supplement
these responses as discovery proceeds.

Please note that all responses are due within 30 days of service. Any
objections not raised within this period are waived.""",
    },
    {
        "title": "Client Communication — Status Update",
        "originator": OriginatorType.OWN,
        "content": """CLIENT STATUS UPDATE

Dear Client,

I am writing to provide you with an update on the current status of your case.

The Court has issued its Order on the Motion to Compel, largely in our favor.
The opposing party has until March 15 to produce the requested documents.

We have also received the opposing counsel's settlement demand of EUR 185,000.
I believe this is significantly above the realistic value of the claim, and
I recommend we prepare a counteroffer in the range of EUR 75,000-95,000.

Next steps:
- Review the produced documents when received (expected by March 15)
- Prepare counteroffer by February 28
- Schedule mediation session for April

Please call me at your earliest convenience to discuss.""",
    },
    {
        "title": "Expert Witness Report — Financial Valuation",
        "originator": OriginatorType.OPPOSING,
        "content": """EXPERT WITNESS REPORT

Prepared by: Dr. Klaus Weber, CPA
Date: January 15, 2026

EXECUTIVE SUMMARY

Based on my analysis of the financial records provided, I have determined
the following:

1. The fair market value of the disputed assets is approximately EUR 420,000.
2. The depreciation schedule used by the Plaintiff is inconsistent with
   industry standards.
3. Additional H&M clothing and retail expenses of EUR 12,500.00 were
   improperly classified as business expenses.

METHODOLOGY

I applied the income approach and market approach to valuation, using
comparable transactions from the past 36 months. The discount rate of 8.5%
reflects the risk profile of the subject entity.

FEE SCHEDULE

My fees for this engagement are calculated at EUR 350.00 per hour under
JVEG guidelines, for a total of EUR 14,000.00 (40 hours).""",
    },
    {
        "title": "Court Judgment — Partial Summary",
        "originator": OriginatorType.COURT,
        "content": """JUDGMENT ON PARTIAL SUMMARY JUDGMENT

After careful consideration of the motions, briefs, and oral argument
presented on January 20, 2026, the Court rules as follows:

1. Plaintiff's Motion for Partial Summary Judgment is GRANTED in part.
   The Court finds that Defendant breached the contractual obligation
   under Section 4.2 of the Agreement.

2. Defendant's Cross-Motion is DENIED.

3. Damages are set at EUR 95,000.00, plus prejudgment interest at the
   statutory rate of 5% per annum from the date of breach.

4. Court costs of EUR 1,250.00 are assessed against Defendant.

5. The remaining claims shall proceed to trial, scheduled for June 10, 2026.

This is a final and appealable Order as to the claims resolved herein.""",
    },
    {
        "title": "Invoice — Legal Services Rendered",
        "originator": OriginatorType.OWN,
        "content": """INVOICE FOR LEGAL SERVICES

Matter: ADV-992-K
Period: January 2026
Attorney: Julian Vance

DESCRIPTION                          HOURS    RATE      AMOUNT
───────────────────────────────────────────────────────────────
Review of discovery responses         3.5    450.00    1,575.00
Draft Motion for Protective Order     5.0    450.00    2,250.00
Client conference call                1.0    450.00      450.00
Legal research on privilege issues    2.5    450.00    1,125.00
Correspondence with opposing counsel  1.5    450.00      675.00
───────────────────────────────────────────────────────────────
SUBTOTAL                                               6,075.00
VAT (19%)                                              1,154.25
───────────────────────────────────────────────────────────────
TOTAL                                                  7,229.25

Payment due within 30 days.""",
    },
    {
        "title": "Deposition Transcript — Key Witness",
        "originator": OriginatorType.COURT,
        "content": """DEPOSITION TRANSCRIPT

Case No.: 2024-FL-DR-00992
Deponent: Maria Schmidt
Date: February 5, 2026
Court Reporter: Lisa Bauer

EXAMINATION BY COUNSEL FOR PLAINTIFF:

Q: Please state your name for the record.
A: Maria Schmidt.
Q: What is your relationship to the Defendant?
A: I was the financial advisor for the Defendant from 2019 to 2023.
Q: Did you prepare any financial statements during that period?
A: Yes, I prepared quarterly and annual statements.
Q: Were these statements provided to the Plaintiff?
A: Not to my knowledge. The Defendant instructed me not to share them.

[Deposition continues for 47 pages...]

The deposition concluded at 3:45 PM. The witness was excused.

Court reporter fee: EUR 890.00 (JVEG witness compensation schedule).""",
    },
]

TITLE_VARIATIONS = [
    "Amended {title}",
    "Supplemental {title}",
    "Re: {title}",
    "{title} — Follow-Up",
    "{title} (Second Filing)",
    "Corrected {title}",
    "{title} — Addendum",
]

COST_TEMPLATES = [
    {
        "title": "Court Filing Fee — Motion to Compel",
        "category": CostCategory.GERICHTSKOSTEN,
        "rvg_position": "KV 1210 GKG",
        "amount_net": 357.14,
        "vat_rate": 0.19,
        "streitwert": 150000,
        "gebuehren_faktor": 1.0,
        "is_reimbursable": True,
    },
    {
        "title": "Attorney Fees — Discovery Phase",
        "category": CostCategory.ANWALTSKOSTEN,
        "rvg_position": "Nr. 3100 VV RVG",
        "amount_net": 4200.00,
        "vat_rate": 0.19,
        "streitwert": 150000,
        "gebuehren_faktor": 1.3,
        "is_reimbursable": True,
    },
    {
        "title": "Opposing Counsel — Costs to Date",
        "category": CostCategory.ANWALTSKOSTEN_GEGNER,
        "rvg_position": "§91 ZPO",
        "amount_net": 6800.00,
        "vat_rate": 0.19,
        "streitwert": 150000,
        "gebuehren_faktor": 1.5,
        "is_reimbursable": False,
    },
    {
        "title": "Expert Witness — Dr. Weber Valuation",
        "category": CostCategory.SACHVERSTAENDIGER,
        "rvg_position": "§3 JVEG",
        "amount_net": 11764.71,
        "vat_rate": 0.19,
        "streitwert": 150000,
        "gebuehren_faktor": None,
        "is_reimbursable": True,
    },
    {
        "title": "Court Advance Payment",
        "category": CostCategory.VORSCHUSS,
        "rvg_position": "KV 1211 GKG",
        "amount_net": 2100.00,
        "vat_rate": 0.0,
        "streitwert": 150000,
        "gebuehren_faktor": None,
        "is_reimbursable": True,
    },
    {
        "title": "Deposition Court Reporter Fees",
        "category": CostCategory.AUSLAGEN,
        "rvg_position": "Nr. 7002 VV RVG",
        "amount_net": 747.90,
        "vat_rate": 0.19,
        "streitwert": None,
        "gebuehren_faktor": None,
        "is_reimbursable": True,
    },
]

DEADLINE_TEMPLATES = [
    ("Response to Discovery Requests", 14),
    ("File Motion for Protective Order", 21),
    ("Submit Expert Witness List", 30),
    ("Settlement Counteroffer Due", 7),
    ("Deposition of Maria Schmidt", 45),
    ("Brief for Summary Judgment", 35),
    ("Mediation Session", 60),
    ("Amended Complaint Filing", 28),
]

HEARING_TEMPLATES = [
    ("Preliminary Injunction Hearing", "Amtsgericht Berlin, Room 2A"),
    ("Summary Judgment Hearing", "Landgericht München, Room 4B"),
    ("Status Conference", "OLG Hamburg, Room 12"),
    ("Mediation Session", "Mediation Center Berlin"),
    ("Trial — Day 1", "BGH Leipzig, Saal 1"),
]

# ── Generate Documents ──────────────────────────────────────────────────────
random.seed(42)
now = datetime.now(timezone.utc).replace(second=0, microsecond=0)
doc_counter = 0
parent_docs = {}  # case_id -> list of doc ids that can be parents

for case_seed in SEED_CASES:
    case_id = case_seed["id"]
    num_docs = random.randint(10, 16)
    parent_docs[case_id] = []

    for i in range(num_docs):
        template = random.choice(DOCUMENT_TEMPLATES)
        originator = template["originator"]
        sender = random.choice(SENDERS[originator])

        # Vary the title occasionally
        if random.random() < 0.3:
            title = random.choice(TITLE_VARIATIONS).format(title=template["title"])
        else:
            title = template["title"]

        content = normalize_hm(template["content"])

        # Random date within last 120 days
        days_ago = random.randint(0, 120)
        received_date = now - timedelta(days=days_ago)

        doc = Document(
            title=title,
            content=content,
            case_id=case_id,
            originator_type=originator,
            sender=sender,
            received_date=received_date,
            needs_review=random.random() < 0.15,
            review_reasons=random.sample(
                [
                    "missing_case_id",
                    "missing_originator",
                    "missing_sender",
                    "missing_received_date",
                ],
                k=random.randint(0, 1),
            )
            if random.random() < 0.1
            else [],
            ai_summary_status="pending",
        )
        db.add(doc)
        db.flush()
        doc_counter += 1

        # ~30% of docs become parents for child documents
        if random.random() < 0.3:
            parent_docs[case_id].append(doc.id)

    db.commit()

    # ── Create Child Documents ──────────────────────────────────────────────
    for parent_id in parent_docs[case_id]:
        if random.random() < 0.6:  # 60% of parents get children
            num_children = random.randint(1, 3)
            for _ in range(num_children):
                template = random.choice(DOCUMENT_TEMPLATES)
                originator = template["originator"]
                sender = random.choice(SENDERS[originator])

                child_title = f"Re: {template['title']} — Response"
                child_content = normalize_hm(
                    f"Response to the above-referenced document.\n\n"
                    f"From: {sender}\n\n"
                    f"{template['content'][:300]}..."
                )

                days_ago = random.randint(0, 90)
                received_date = now - timedelta(days=days_ago)

                child = Document(
                    title=child_title,
                    content=child_content,
                    case_id=case_id,
                    originator_type=originator,
                    sender=sender,
                    received_date=received_date,
                    parent_id=parent_id,
                    needs_review=False,
                    ai_summary_status="pending",
                )
                db.add(child)
                db.flush()
                doc_counter += 1

    db.commit()

    # ── Seed Deadlines ──────────────────────────────────────────────────────
    for title, offset in random.sample(DEADLINE_TEMPLATES, k=random.randint(3, 6)):
        due_date = now + timedelta(days=random.randint(-10, 60))
        deadline = Deadline(
            case_id=case_id,
            title=title,
            description=f"Deadline for {title.lower()} in case {case_id}.",
            due_at=due_date,
            completed=due_date < now and random.random() < 0.3,
        )
        db.add(deadline)

    # ── Seed Hearings ───────────────────────────────────────────────────────
    for title, location in random.sample(HEARING_TEMPLATES, k=random.randint(2, 4)):
        hearing_date = now + timedelta(days=random.randint(5, 90))
        hearing = Hearing(
            case_id=case_id,
            title=title,
            description=f"Hearing: {title}",
            location=location,
            scheduled_for=hearing_date.replace(
                hour=random.choice([9, 10, 11, 14, 15]),
                minute=random.choice([0, 15, 30, 45]),
            ),
        )
        db.add(hearing)

    # ── Seed Costs ──────────────────────────────────────────────────────────
    for cost_template in random.sample(COST_TEMPLATES, k=random.randint(3, 5)):
        issued_offset = random.randint(-90, -5)
        due_offset = random.randint(-30, 30)
        status = random.choice(
            [
                CostStatus.OFFEN,
                CostStatus.BEZAHLT,
                CostStatus.ERSTATTET,
                CostStatus.TEILWEISE,
            ]
        )

        cost = LegalCost(
            case_id=case_id,
            title=cost_template["title"],
            category=cost_template["category"],
            status=status,
            rvg_position=cost_template["rvg_position"],
            amount_net=cost_template["amount_net"],
            vat_rate=cost_template["vat_rate"],
            amount_gross=cost_template["amount_net"] * (1 + cost_template["vat_rate"]),
            streitwert=cost_template["streitwert"],
            gebuehren_faktor=cost_template["gebuehren_faktor"],
            is_reimbursable=cost_template["is_reimbursable"],
            issued_at=now + timedelta(days=issued_offset),
            due_at=now + timedelta(days=due_offset),
            paid_at=now + timedelta(days=random.randint(-60, -1))
            if status == CostStatus.BEZAHLT
            else None,
            amount_paid=cost_template["amount_net"] * (1 + cost_template["vat_rate"])
            if status == CostStatus.BEZAHLT
            else 0,
            amount_reimbursed=cost_template["amount_net"]
            * (1 + cost_template["vat_rate"])
            if status == CostStatus.ERSTATTET
            else 0,
        )
        db.add(cost)

    db.commit()

# ── Summary ─────────────────────────────────────────────────────────────────
total_docs = db.query(Document).count()
total_cases = db.query(Case).count()
total_deadlines = db.query(Deadline).count()
total_hearings = db.query(Hearing).count()
total_costs = db.query(LegalCost).count()
parent_count = db.query(Document).filter(Document.parent_id.isnot(None)).count()

print(f"Seed complete:")
print(f"  Cases:      {total_cases}")
print(f"  Documents:  {total_docs}")
print(f"  Children:   {parent_count}")
print(f"  Deadlines:  {total_deadlines}")
print(f"  Hearings:   {total_hearings}")
print(f"  Costs:      {total_costs}")

db.close()
