from datetime import datetime, timedelta
from itertools import groupby
from urllib.parse import unquote
from fastapi import APIRouter, Depends, Request
from sqlalchemy.orm import Session

from app.constants import (
    CASE_STATUS_META,
    COST_CATEGORY_META,
    COST_STATUS_META,
    ORIGINATOR_COLORS,
    ORIGINATOR_ICONS,
)
from app.dependencies import get_db
from app.helpers import (
    render_page,
    format_relative_time,
    format_upcoming_datetime,
    format_deadline_badge,
    format_form_datetime,
    load_case_schedule,
    build_document_extraction_context,
    build_cost_summary,
)
from app.models.database import (
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

router = APIRouter()


@router.get("/")
async def dashboard(request: Request, db: Session = Depends(get_db)):
    week_ago = datetime.utcnow() - timedelta(days=7)
    now = datetime.utcnow()

    all_cases = db.query(Case).order_by(Case.created_at.desc()).all()
    case_titles = {case.id: case.title for case in all_cases}

    active_cases = [case for case in all_cases if case.status != CaseStatus.CLOSED]
    active_case_count = len(active_cases)
    new_active_cases_this_week = sum(
        1 for case in active_cases if case.created_at >= week_ago
    )

    pending_docs = (
        db.query(Document)
        .filter(Document.needs_review == True)
        .order_by(Document.created_at.desc())
        .all()
    )
    pending_review_count = len(pending_docs)
    pending_added_this_week = sum(
        1 for doc in pending_docs if doc.created_at >= week_ago
    )

    court_doc_count = (
        db.query(Document)
        .filter(Document.originator_type == OriginatorType.COURT)
        .count()
    )
    new_documents_this_week = (
        db.query(Document).filter(Document.created_at >= week_ago).count()
    )

    priority_docs = pending_docs[:4]
    recent_documents = (
        db.query(Document).order_by(Document.created_at.desc()).limit(4).all()
    )
    active_case_snapshot = active_cases[:4]
    upcoming_deadlines = (
        db.query(Deadline)
        .filter(Deadline.completed == False, Deadline.due_at >= now)
        .order_by(Deadline.due_at.asc())
        .limit(4)
        .all()
    )
    upcoming_hearings = (
        db.query(Hearing)
        .filter(Hearing.scheduled_for >= now)
        .order_by(Hearing.scheduled_for.asc())
        .limit(3)
        .all()
    )

    # Overdue costs
    overdue_costs = (
        db.query(LegalCost)
        .filter(
            LegalCost.due_at < now,
            LegalCost.status.notin_([CostStatus.BEZAHLT, CostStatus.ERSTATTET]),
        )
        .order_by(LegalCost.due_at.asc())
        .limit(4)
        .all()
    )

    return render_page(
        request,
        "pages/dashboard.html",
        db=db,
        active_case_count=active_case_count,
        new_active_cases_this_week=new_active_cases_this_week,
        pending_review_count=pending_review_count,
        pending_added_this_week=pending_added_this_week,
        court_doc_count=court_doc_count,
        new_documents_this_week=new_documents_this_week,
        priority_docs=priority_docs,
        recent_documents=recent_documents,
        active_case_snapshot=active_case_snapshot,
        upcoming_deadlines=upcoming_deadlines,
        upcoming_hearings=upcoming_hearings,
        overdue_costs=overdue_costs,
        case_titles=case_titles,
        originator_colors=ORIGINATOR_COLORS,
        originator_icons=ORIGINATOR_ICONS,
        status_meta=CASE_STATUS_META,
        format_relative_time=format_relative_time,
        format_upcoming_datetime=format_upcoming_datetime,
        format_deadline_badge=format_deadline_badge,
    )


@router.get("/cases")
async def case_directory(request: Request, db: Session = Depends(get_db)):
    all_cases = db.query(Case).order_by(Case.created_at.desc()).all()

    doc_case_ids = {
        r[0]
        for r in db.query(Document.case_id)
        .filter(Document.case_id != None)
        .distinct()
        .all()
    }
    existing_ids = {c.id for c in all_cases}
    for cid in doc_case_ids - existing_ids:
        new_case = Case(id=cid, title=f"Case {cid}", status=CaseStatus.INTAKE)
        db.add(new_case)
        all_cases.append(new_case)
    if doc_case_ids - existing_ids:
        db.commit()

    active_cases = [c for c in all_cases if c.status != CaseStatus.CLOSED]
    closed_cases = [c for c in all_cases if c.status == CaseStatus.CLOSED]

    return render_page(
        request,
        "pages/case_directory.html",
        db=db,
        active_cases=active_cases,
        closed_cases=closed_cases,
        status_meta=CASE_STATUS_META,
    )


@router.get("/cases/{case_id}")
async def case_stream(request: Request, case_id: str, db: Session = Depends(get_db)):
    review_docs = (
        db.query(Document)
        .filter(Document.case_id == case_id, Document.needs_review == True)
        .order_by(Document.created_at.desc())
        .all()
    )

    chrono_docs = (
        db.query(Document)
        .filter(
            Document.case_id == case_id,
            Document.parent_id == None,
            Document.needs_review == False,
        )
        .order_by(Document.created_at.desc())
        .all()
    )

    case = db.get(Case, case_id)
    case_title = case.title if case else f"Case {case_id}"
    court_id = case.court_id if case and case.court_id else ""
    case_status = case.status if case else CaseStatus.INTAKE
    schedule = load_case_schedule(db, case_id)

    # Load case costs
    case_costs_list = (
        db.query(LegalCost)
        .filter(LegalCost.case_id == case_id)
        .order_by(LegalCost.issued_at.asc())
        .all()
    )
    case_costs = None
    if case_costs_list:
        case_costs = {
            "costs": case_costs_list,
            "summary": build_cost_summary(case_costs_list, CostStatus),
        }

    return render_page(
        request,
        "pages/case_stream.html",
        db=db,
        review_docs=review_docs,
        documents=chrono_docs,
        case_id=case_id,
        case_title=case_title,
        court_id=court_id,
        case_status=case_status,
        upcoming_deadlines=schedule["upcoming_deadlines"],
        completed_deadlines=schedule["completed_deadlines"],
        upcoming_hearings=schedule["upcoming_hearings"],
        past_hearings=schedule["past_hearings"],
        case_costs=case_costs,
        originator_colors=ORIGINATOR_COLORS,
        originator_icons=ORIGINATOR_ICONS,
        status_meta=CASE_STATUS_META,
        cost_category_meta=COST_CATEGORY_META,
        cost_status_meta=COST_STATUS_META,
        format_upcoming_datetime=format_upcoming_datetime,
        format_form_datetime=format_form_datetime,
    )


@router.get("/triage")
async def triage_center(request: Request, db: Session = Depends(get_db)):
    documents = (
        db.query(Document)
        .filter(Document.needs_review == True)
        .order_by(Document.created_at.desc())
        .all()
    )
    return render_page(
        request,
        "pages/triage.html",
        db=db,
        documents=documents,
        originator_colors=ORIGINATOR_COLORS,
        originator_icons=ORIGINATOR_ICONS,
    )


@router.get("/timeline")
async def master_timeline(request: Request, db: Session = Depends(get_db)):
    all_docs = (
        db.query(Document)
        .filter(Document.parent_id == None)
        .order_by(Document.created_at.desc())
        .all()
    )

    def period_key(doc):
        return doc.created_at.strftime("%B %Y")

    grouped = []
    for key, group in groupby(all_docs, key=period_key):
        grouped.append((key, list(group)))

    total_docs = db.query(Document).count()
    pending_count = db.query(Document).filter(Document.needs_review == True).count()

    cases = {c.id: c.title for c in db.query(Case).all()}

    return render_page(
        request,
        "pages/timeline.html",
        db=db,
        grouped_docs=grouped,
        total_docs=total_docs,
        pending_count=pending_count,
        case_titles=cases,
        originator_colors=ORIGINATOR_COLORS,
        originator_icons=ORIGINATOR_ICONS,
    )


@router.get("/costs")
async def legal_costs(request: Request, db: Session = Depends(get_db)):
    all_cases = db.query(Case).order_by(Case.created_at.desc()).all()
    all_costs = (
        db.query(LegalCost).order_by(LegalCost.case_id, LegalCost.issued_at.asc()).all()
    )

    now = datetime.utcnow()
    seven_days = timedelta(days=7)
    overdue_costs = [
        c
        for c in all_costs
        if c.due_at
        and c.due_at < now
        and c.status not in (CostStatus.BEZAHLT, CostStatus.ERSTATTET)
    ]
    upcoming_costs = [
        c for c in all_costs if c.due_at and now <= c.due_at <= now + seven_days
    ]

    costs_by_case = {}
    for case in all_cases:
        case_costs = [c for c in all_costs if c.case_id == case.id]
        if not case_costs:
            continue
        costs_by_case[case.id] = {
            "case": case,
            "costs": case_costs,
            "summary": build_cost_summary(case_costs, CostStatus),
            "streitwert": next(
                (c.streitwert for c in case_costs if c.streitwert), None
            ),
        }

    global_summary = build_cost_summary(all_costs, CostStatus)
    case_titles = {c.id: c.title for c in all_cases}

    return render_page(
        request,
        "pages/costs.html",
        db=db,
        costs_by_case=costs_by_case,
        global_summary=global_summary,
        overdue_costs=overdue_costs,
        upcoming_costs=upcoming_costs,
        cost_category_meta=COST_CATEGORY_META,
        cost_status_meta=COST_STATUS_META,
        status_meta=CASE_STATUS_META,
        case_titles=case_titles,
    )


@router.get("/costs/new")
async def cost_form(request: Request, db: Session = Depends(get_db)):
    all_cases = (
        db.query(Case)
        .filter(Case.status != CaseStatus.CLOSED)
        .order_by(Case.created_at.desc())
        .all()
    )
    return render_page(
        request,
        "partials/cost_form.html",
        db=db,
        all_cases=all_cases,
        cost_category_meta=COST_CATEGORY_META,
    )


@router.get("/contacts")
async def contacts(request: Request, db: Session = Depends(get_db)):
    # Fetch all documents with non-null sender
    docs = db.query(Document).filter(Document.sender.isnot(None)).all()

    # Aggregate by sender
    contacts_dict = {}
    for doc in docs:
        sender = doc.sender.strip()
        if sender not in contacts_dict:
            contacts_dict[sender] = {
                "name": sender,
                "originator_type": doc.originator_type,
                "doc_count": 0,
                "case_ids": set(),
                "needs_review_count": 0,
                "first_contact": doc.received_date or doc.created_at,
                "last_contact": doc.received_date or doc.created_at,
                "recent_docs": [],
            }

        contact = contacts_dict[sender]
        contact["doc_count"] += 1
        if doc.case_id:
            contact["case_ids"].add(doc.case_id)
        if doc.needs_review:
            contact["needs_review_count"] += 1

        # Track first and last contact
        contact_date = doc.received_date or doc.created_at
        if contact_date < contact["first_contact"]:
            contact["first_contact"] = contact_date
        if contact_date > contact["last_contact"]:
            contact["last_contact"] = contact_date

        # Keep recent docs (most recent first)
        contact["recent_docs"].append(
            {
                "id": doc.id,
                "title": doc.title,
                "date": doc.received_date or doc.created_at,
                "case_id": doc.case_id,
            }
        )

    # Sort recent docs and keep top 3
    for contact in contacts_dict.values():
        contact["recent_docs"].sort(key=lambda x: x["date"], reverse=True)
        contact["recent_docs"] = contact["recent_docs"][:3]

    # Convert to sorted list
    contacts_list = sorted(
        contacts_dict.values(),
        key=lambda x: x["last_contact"],
        reverse=True,
    )

    # Build summary counters
    summary = {
        "total": len(contacts_list),
        "court": sum(
            1 for c in contacts_list if c["originator_type"] == OriginatorType.COURT
        ),
        "opposing": sum(
            1 for c in contacts_list if c["originator_type"] == OriginatorType.OPPOSING
        ),
        "own": sum(
            1 for c in contacts_list if c["originator_type"] == OriginatorType.OWN
        ),
    }

    return render_page(
        request,
        "pages/contacts.html",
        db=db,
        contacts=contacts_list,
        summary=summary,
        originator_colors=ORIGINATOR_COLORS,
        originator_icons=ORIGINATOR_ICONS,
        case_titles={case.id: case.title for case in db.query(Case).all()},
    )


@router.get("/contacts/{sender_name}")
async def contact_detail(
    request: Request, sender_name: str, db: Session = Depends(get_db)
):
    sender = unquote(sender_name)

    # Fetch all documents from this sender
    docs = (
        db.query(Document)
        .filter(Document.sender == sender)
        .order_by(Document.received_date.desc(), Document.created_at.desc())
        .all()
    )

    if not docs:
        return render_page(
            request,
            "partials/empty_state.html",
            db=db,
            icon="person",
            title="Contact Not Found",
            body="No documents from this contact.",
        )

    # Build contact data
    contact = {
        "name": sender,
        "originator_type": docs[0].originator_type,  # Use first doc's type as primary
        "doc_count": len(docs),
        "case_ids": set(),
        "needs_review_count": 0,
        "first_contact": docs[-1].received_date or docs[-1].created_at,  # Oldest
        "last_contact": docs[0].received_date or docs[0].created_at,  # Newest
        "docs": docs,
    }

    for doc in docs:
        if doc.case_id:
            contact["case_ids"].add(doc.case_id)
        if doc.needs_review:
            contact["needs_review_count"] += 1

    case_titles = {case.id: case.title for case in db.query(Case).all()}

    return render_page(
        request,
        "partials/contact_detail.html",
        db=db,
        contact=contact,
        case_titles=case_titles,
        originator_colors=ORIGINATOR_COLORS,
        originator_icons=ORIGINATOR_ICONS,
    )


@router.get("/document/{doc_id}")
async def get_document_details(
    request: Request, doc_id: int, db: Session = Depends(get_db)
):
    doc = db.query(Document).filter(Document.id == doc_id).first()
    extraction_context = build_document_extraction_context(db, doc)
    return render_page(
        request,
        "partials/document_details.html",
        db=db,
        doc_id=doc_id,
        doc=doc,
        format_upcoming_datetime=format_upcoming_datetime,
        format_form_datetime=format_form_datetime,
        **extraction_context,
    )


@router.get("/document/{doc_id}/extractions")
async def render_document_extraction_panel(
    request: Request,
    doc_id: int,
    db: Session = Depends(get_db),
):
    doc = db.query(Document).filter(Document.id == doc_id).first()
    extraction_context = build_document_extraction_context(db, doc)
    return render_page(
        request,
        "partials/document_extraction_panel.html",
        db=db,
        doc=doc,
        format_upcoming_datetime=format_upcoming_datetime,
        format_form_datetime=format_form_datetime,
        **extraction_context,
    )
