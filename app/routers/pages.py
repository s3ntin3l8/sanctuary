import logging
from datetime import UTC, datetime, timedelta
from itertools import groupby
from urllib.parse import unquote

from fastapi import APIRouter, Depends, Request
from fastapi.responses import HTMLResponse
from sqlalchemy import or_
from sqlalchemy.orm import Session

logger = logging.getLogger(__name__)

from app.config import OLLAMA_BASE_URL, OLLAMA_EMBED_MODEL, templates
from app.constants import (
    CASE_STATUS_META,
    COST_CATEGORY_META,
    COST_STATUS_META,
    ORIGINATOR_COLORS,
    ORIGINATOR_ICONS,
)
from app.dependencies import get_db
from app.helpers import (
    build_cost_summary,
    build_document_extraction_context,
    format_deadline_badge,
    format_form_datetime,
    format_relative_time,
    format_upcoming_datetime,
    load_case_schedule,
    render_page,
)
from app.models.database import (
    Case,
    CaseStatus,
    CostStatus,
    Deadline,
    Document,
    Entity,
    EntityType,
    Hearing,
    LegalCost,
    OriginatorType,
)
from app.services.ai_summary import check_ollama_status
from app.services.embeddings import check_embedding_status

router = APIRouter()


@router.get("/")
async def dashboard(request: Request, db: Session = Depends(get_db)):
    now = datetime.utcnow()
    week_ago = now - timedelta(days=7)

    case_titles = {c.id: c.title for c in db.query(Case.id, Case.title).all()}
    all_cases = db.query(Case).order_by(Case.created_at.desc()).all()

    active_cases = [case for case in all_cases if case.status != CaseStatus.CLOSED]
    active_case_count = len(active_cases)
    new_active_cases_this_week = sum(
        1
        for case in active_cases
        if case.created_at
        and (
            case.created_at.replace(tzinfo=UTC)
            if case.created_at.tzinfo is None
            else case.created_at
        )
        >= week_ago.replace(tzinfo=UTC)
    )

    pending_docs = (
        db.query(Document)
        .filter(Document.needs_review)
        .order_by(Document.created_at.desc())
        .all()
    )
    pending_review_count = len(pending_docs)
    pending_added_this_week = sum(
        1
        for doc in pending_docs
        if doc.created_at
        and (
            doc.created_at.replace(tzinfo=UTC)
            if doc.created_at.tzinfo is None
            else doc.created_at
        )
        >= week_ago.replace(tzinfo=UTC)
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
        .filter(~Deadline.completed, Deadline.due_at >= now)
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

    status_summary = await check_ollama_status()
    embed_status = await check_embedding_status()
    ai_status = {
        "reachable": status_summary["reachable"],
        "summary_model": status_summary["summary_model"],
        "embedding_model": embed_status["embedding_model"],
        "error": status_summary["error"] or embed_status["error"],
    }

    return render_page(
        request,
        "pages/dashboard.html",
        db=db,
        ai_status=ai_status,
        ollama_base_url=OLLAMA_BASE_URL,
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


@router.get("/activity")
async def activity_log(
    request: Request,
    db: Session = Depends(get_db),
    limit: int = 20,
    offset: int = 0,
):
    # Get total counts
    total_count = db.query(Document).count()
    court_count = (
        db.query(Document)
        .filter(Document.originator_type == OriginatorType.COURT)
        .count()
    )
    pending_count = db.query(Document).filter(Document.needs_review).count()
    case_count = (
        db.query(Document.case_id)
        .filter(Document.case_id is not None, Document.case_id != "_TRIAGE")
        .distinct()
        .count()
    )

    # Get paginated documents
    documents = (
        db.query(Document)
        .order_by(Document.created_at.desc())
        .limit(limit + 1)  # Fetch one extra to check if there's more
        .offset(offset)
        .all()
    )

    has_more = len(documents) > limit
    if has_more:
        documents = documents[:limit]

    case_titles = {c.id: c.title for c in db.query(Case.id, Case.title).all()}

    return render_page(
        request,
        "pages/activity_log.html",
        db=db,
        documents=documents,
        total_count=total_count,
        court_count=court_count,
        pending_count=pending_count,
        case_count=case_count,
        case_titles=case_titles,
        has_more=has_more,
        per_page=limit,
        offset=offset,
        originator_colors=ORIGINATOR_COLORS,
        originator_icons=ORIGINATOR_ICONS,
    )


@router.get("/cases")
async def case_directory(request: Request, db: Session = Depends(get_db)):
    # Exclude _TRIAGE from case directory - it's a virtual inbox, not a real case
    all_cases = (
        db.query(Case)
        .filter(Case.id != "_TRIAGE")
        .order_by(Case.created_at.desc())
        .all()
    )

    doc_case_ids = {
        r[0]
        for r in db.query(Document.case_id)
        .filter(Document.case_id is not None, Document.case_id != "_TRIAGE")
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


@router.get("/cases/{case_id:path}")
async def case_stream(request: Request, case_id: str, db: Session = Depends(get_db)):
    review_docs = (
        db.query(Document)
        .filter(Document.case_id == case_id, Document.needs_review)
        .order_by(Document.created_at.desc())
        .all()
    )

    chrono_docs = (
        db.query(Document)
        .filter(
            Document.case_id == case_id,
            Document.parent_id is None,
            not Document.needs_review,
        )
        .order_by(Document.created_at.desc())
        .all()
    )

    case = db.get(Case, case_id)
    case_title = case.title if case else f"Case {case_id}"
    court_id = case.court_id if case and case.court_id else ""
    case_status = case.status if case else CaseStatus.INTAKE
    schedule = load_case_schedule(db, case_id)

    top_level_docs = (
        db.query(Document)
        .filter(Document.case_id == case_id, Document.parent_id is None)
        .order_by(Document.created_at.desc())
        .all()
    )

    # Group resolved top-level docs by month for the parent picker
    resolved_docs = [d for d in top_level_docs if not d.needs_review]
    resolved_by_month = {}
    for doc in resolved_docs:
        dt = doc.created_at or doc.received_date
        month_key = dt.strftime("%B %Y") if dt else "Unknown"
        if month_key not in resolved_by_month:
            resolved_by_month[month_key] = []
        resolved_by_month[month_key].append(doc)

    # Sort months chronologically
    def _month_sort_key(m):
        if m == "Unknown":
            return datetime.min.replace(tzinfo=UTC)
        try:
            return datetime.strptime(m, "%B %Y").replace(tzinfo=UTC)
        except ValueError:
            return datetime.min.replace(tzinfo=UTC)

    resolved_by_month = dict(
        sorted(
            resolved_by_month.items(), key=lambda x: _month_sort_key(x[0]), reverse=True
        )
    )

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

    # Load case entities grouped by type
    entities_raw = db.query(Entity).filter(Entity.case_id == case_id).all()
    entities = {
        "persons": [],
        "organizations": [],
        "dates": [],
        "financial": [],
        "legal_categories": [],
    }
    for e in entities_raw:
        if e.type == EntityType.PERSON:
            entities["persons"].append(e)
        elif e.type == EntityType.ORGANIZATION:
            entities["organizations"].append(e)
        elif e.type == EntityType.DATE:
            entities["dates"].append(e)
        elif e.type == EntityType.FINANCIAL:
            entities["financial"].append(e)
        elif e.type == EntityType.LEGAL_CATEGORY:
            entities["legal_categories"].append(e)

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
        entities=entities,
        top_level_docs=top_level_docs,
        resolved_by_month=resolved_by_month,
        originator_colors=ORIGINATOR_COLORS,
        originator_icons=ORIGINATOR_ICONS,
        status_meta=CASE_STATUS_META,
        cost_category_meta=COST_CATEGORY_META,
        cost_status_meta=COST_STATUS_META,
        format_upcoming_datetime=format_upcoming_datetime,
        format_form_datetime=format_form_datetime,
    )


@router.get("/triage")
async def triage_center(
    request: Request,
    db: Session = Depends(get_db),
    limit: int = 50,
    offset: int = 0,
    originator: str | None = None,
    needs_review: bool | None = None,
    search: str | None = None,
):
    # Show all docs that need review (not just _TRIAGE)
    query = db.query(Document).filter(Document.needs_review)

    if originator:
        try:
            orig_type = OriginatorType(originator)
            query = query.filter(Document.originator_type == orig_type)
        except ValueError:
            pass

    if needs_review is not None:
        query = query.filter(Document.needs_review == needs_review)

    if search:
        search_term = f"%{search}%"
        query = query.filter(
            or_(
                Document.title.ilike(search_term),
                Document.sender.ilike(search_term),
                Document.content.ilike(search_term),
            )
        )

    documents = (
        query.order_by(Document.created_at.desc()).limit(limit).offset(offset).all()
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
        .filter(Document.parent_id is None)
        .order_by(Document.created_at.desc())
        .all()
    )

    def period_key(doc):
        return doc.created_at.strftime("%B %Y")

    grouped = []
    for key, group in groupby(all_docs, key=period_key):
        grouped.append((key, list(group)))

    total_docs = db.query(Document).count()
    pending_count = db.query(Document).filter(Document.needs_review).count()

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

    now = datetime.now(UTC)
    seven_days = timedelta(days=7)

    def _make_aware(dt):
        if dt and dt.tzinfo is None:
            return dt.replace(tzinfo=UTC)
        return dt

    overdue_costs = [
        c
        for c in all_costs
        if c.due_at
        and _make_aware(c.due_at) < now
        and c.status not in (CostStatus.BEZAHLT, CostStatus.ERSTATTET)
    ]
    upcoming_costs = [
        c
        for c in all_costs
        if c.due_at and now <= _make_aware(c.due_at) <= now + seven_days
    ]

    costs_by_case_id = {}
    for c in all_costs:
        cid = c.case_id
        if cid not in costs_by_case_id:
            costs_by_case_id[cid] = []
        costs_by_case_id[cid].append(c)

    costs_by_case = {}
    for case in all_cases:
        case_costs = costs_by_case_id.get(case.id, [])
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


@router.get("/cases/{case_id}/costs/new")
async def case_cost_form(request: Request, case_id: str, db: Session = Depends(get_db)):
    case = db.query(Case).filter(Case.id == case_id).first()
    return render_page(
        request,
        "partials/cost_form.html",
        db=db,
        preselected_case_id=case_id,
        preselected_case_title=case.title if case else case_id,
        cost_category_meta=COST_CATEGORY_META,
    )


@router.get("/entities")
async def global_entities(request: Request, db: Session = Depends(get_db)):
    all_entities = db.query(Entity).all()
    grouped_entities = {}
    for entity in all_entities:
        if entity.type not in grouped_entities:
            grouped_entities[entity.type] = {}
        if entity.name not in grouped_entities[entity.type]:
            grouped_entities[entity.type][entity.name] = 0
        grouped_entities[entity.type][entity.name] += 1

    # Sort groups by count descending
    for type_key in grouped_entities:
        grouped_entities[type_key] = dict(
            sorted(
                grouped_entities[type_key].items(),
                key=lambda item: item[1],
                reverse=True,
            )
        )

    return render_page(
        request,
        "pages/entities.html",
        db=db,
        grouped_entities=grouped_entities,
    )


@router.get("/contacts")
async def contacts(request: Request, db: Session = Depends(get_db)):
    # Fetch all documents with non-null sender
    docs = db.query(Document).filter(Document.sender.isnot(None)).all()

    # Aggregate by sender
    contacts_dict = {}
    for doc in docs:
        sender = doc.sender.strip()
        initial_date = doc.received_date or doc.created_at
        if initial_date and initial_date.tzinfo is None:
            initial_date = initial_date.replace(tzinfo=UTC)

        if sender not in contacts_dict:
            contacts_dict[sender] = {
                "name": sender,
                "originator_type": doc.originator_type,
                "doc_count": 0,
                "case_ids": set(),
                "needs_review_count": 0,
                "first_contact": initial_date,
                "last_contact": initial_date,
                "recent_docs": [],
            }

        contact = contacts_dict[sender]
        contact["doc_count"] += 1
        if doc.case_id:
            contact["case_ids"].add(doc.case_id)
        if doc.needs_review:
            contact["needs_review_count"] += 1

        # Track first and last contact
        contact_date = initial_date

        if contact_date and contact_date < contact["first_contact"]:
            contact["first_contact"] = contact_date
        if contact_date and contact_date > contact["last_contact"]:
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
    from datetime import datetime

    for contact in contacts_dict.values():
        for doc in contact["recent_docs"]:
            if doc["date"] and doc["date"].tzinfo is None:
                doc["date"] = doc["date"].replace(tzinfo=UTC)
        contact["recent_docs"].sort(
            key=lambda x: x["date"] or datetime.min, reverse=True
        )
        contact["recent_docs"] = contact["recent_docs"][:3]

    # Convert to sorted list
    contacts_list = sorted(
        contacts_dict.values(),
        key=lambda x: x["last_contact"] or datetime.min,
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

    case_titles = {c.id: c.title for c in db.query(Case.id, Case.title).all()}

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
    from app.constants import REVIEW_FIELD_LABELS
    from app.models.database import Case, OriginatorType

    doc = db.query(Document).filter(Document.id == doc_id).first()
    cases = db.query(Case).filter(Case.id != "_TRIAGE").order_by(Case.title.asc()).all()
    extraction_context = build_document_extraction_context(db, doc)

    return render_page(
        request,
        "partials/document_details.html",
        db=db,
        doc_id=doc_id,
        doc=doc,
        cases=cases,
        OriginatorType=OriginatorType,
        review_field_labels=REVIEW_FIELD_LABELS,
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


@router.get("/upload")
async def upload_form(
    request: Request, case_id: str = None, db: Session = Depends(get_db)
):
    try:
        top_level_docs = []
        case_title = None
        if case_id:
            case = db.query(Case).filter(Case.id == case_id).first()
            case_title = case.title if case else case_id
            top_level_docs = (
                db.query(Document)
                .filter(Document.case_id == case_id, Document.parent_id.is_(None))
                .order_by(Document.created_at.desc())
                .all()
            )
        return templates.TemplateResponse(
            request,
            "partials/upload_form.html",
            {
                "request": request,
                "case_id": case_id,
                "case_title": case_title,
                "top_level_docs": top_level_docs,
            },
        )
    except Exception as e:
        error_msg = f"Failed to load upload form: {e}"
        return HTMLResponse(
            f'<div class="p-6 text-sm text-error">{error_msg}</div>',
            status_code=500,
        )


@router.get("/api/search")
async def search_api(
    q: str,
    db: Session = Depends(get_db),
    limit: int = 10,
):
    """API endpoint for search autocomplete."""
    if not q or len(q) < 2:
        return {"documents": [], "cases": [], "contacts": [], "total": 0}

    from sqlalchemy import or_, text

    q_like = f"%{q}%"

    docs = None
    try:
        import json

        import httpx

        with httpx.Client(timeout=2.0) as client:
            resp = client.post(
                f"{OLLAMA_BASE_URL}/api/embeddings",
                json={"model": OLLAMA_EMBED_MODEL, "prompt": q},
            )
            resp.raise_for_status()
            emb = resp.json().get("embedding")
            if emb:
                stmt = text("""
                    SELECT id FROM documents
                    WHERE content_embedding IS NOT NULL
            ORDER BY vec_distance_L2(
                vec_f32(json_extract(content_embedding, "$")),
                vec_f32(:emb)
            )
                    LIMIT :limit
                """)
                res = db.execute(
                    stmt, {"emb": json.dumps(emb), "limit": limit}
                ).fetchall()
                doc_ids = [r[0] for r in res]
                if doc_ids:
                    docs_unordered = (
                        db.query(Document).filter(Document.id.in_(doc_ids)).all()
                    )
                    doc_map = {d.id: d for d in docs_unordered}
                    docs = [doc_map[i] for i in doc_ids if i in doc_map]
    except Exception:
        logger.exception("Semantic search failed, falling back to LIKE")

    if docs is None:
        docs = (
            db.query(Document)
            .filter(
                or_(
                    Document.title.ilike(q_like),
                    Document.sender.ilike(q_like),
                    Document.content.ilike(q_like),
                )
            )
            .limit(limit)
            .all()
        )

    cases = (
        db.query(Case)
        .filter(or_(Case.id.ilike(q_like), Case.title.ilike(q_like)))
        .limit(limit)
        .all()
    )

    contacts = (
        db.query(Document.sender)
        .filter(Document.sender.ilike(q_like))
        .distinct()
        .limit(limit)
        .all()
    )

    return {
        "documents": [
            {
                "id": d.id,
                "title": d.title[:80],
                "case_id": d.case_id,
                "sender": d.sender,
            }
            for d in docs
        ],
        "cases": [
            {"id": c.id, "title": c.title, "status": c.status.value} for c in cases
        ],
        "contacts": [{"name": c[0]} for c in contacts if c[0]],
        "total": len(docs) + len(cases) + len(contacts),
    }


@router.get("/search")
async def search_page(
    request: Request,
    q: str,
    db: Session = Depends(get_db),
):
    """Full search results page."""
    from sqlalchemy import or_, text

    if not q or len(q) < 2:
        return render_page(
            request,
            "pages/search.html",
            db=db,
            q=q,
            documents=[],
            cases=[],
            contacts=[],
            total=0,
        )

    q_like = f"%{q}%"

    docs = None
    try:
        import json

        import httpx

        with httpx.Client(timeout=2.0) as client:
            resp = client.post(
                f"{OLLAMA_BASE_URL}/api/embeddings",
                json={"model": OLLAMA_EMBED_MODEL, "prompt": q},
            )
            resp.raise_for_status()
            emb = resp.json().get("embedding")
            if emb:
                stmt = text("""
                    SELECT id FROM documents
                    WHERE content_embedding IS NOT NULL
            ORDER BY vec_distance_L2(
                vec_f32(json_extract(content_embedding, "$")),
                vec_f32(:emb)
            )
                    LIMIT 50
                """)
                res = db.execute(stmt, {"emb": json.dumps(emb)}).fetchall()
                doc_ids = [r[0] for r in res]
                if doc_ids:
                    docs_unordered = (
                        db.query(Document).filter(Document.id.in_(doc_ids)).all()
                    )
                    doc_map = {d.id: d for d in docs_unordered}
                    docs = [doc_map[i] for i in doc_ids if i in doc_map]
    except Exception:
        logger.exception("Semantic search failed, falling back to LIKE")

    if docs is None:
        docs = (
            db.query(Document)
            .filter(
                or_(
                    Document.title.ilike(q_like),
                    Document.sender.ilike(q_like),
                    Document.content.ilike(q_like),
                )
            )
            .all()
        )

    cases = (
        db.query(Case)
        .filter(or_(Case.id.ilike(q_like), Case.title.ilike(q_like)))
        .all()
    )

    contacts = (
        db.query(Document.sender).filter(Document.sender.ilike(q_like)).distinct().all()
    )

    total = len(docs) + len(cases) + len(contacts)

    return render_page(
        request,
        "pages/search.html",
        db=db,
        q=q,
        documents=docs,
        cases=cases,
        contacts=[c[0] for c in contacts if c[0]],
        total=total,
    )


@router.get("/api/activity-feed")
async def activity_feed(
    request: Request,
    db: Session = Depends(get_db),
    limit: int = 20,
    offset: int = 0,
):
    """HTMX endpoint for infinite scroll activity feed."""
    from fastapi.responses import HTMLResponse

    documents = (
        db.query(Document)
        .order_by(Document.created_at.desc())
        .limit(limit)
        .offset(offset)
        .all()
    )

    cases = {c.id: c.title for c in db.query(Case).all()}
    html_parts = []
    for doc in documents:
        stripe_color = ORIGINATOR_COLORS.get(doc.originator_type, "#64748b")
        stripe_icon = ORIGINATOR_ICONS.get(doc.originator_type, "help_outline")

        case_badge = ""
        if doc.case_id and cases.get(doc.case_id):
            title = cases.get(doc.case_id, doc.case_id)
            case_badge = (
                f'<a href="/cases/{doc.case_id}" onclick="event.stopPropagation()" '
                'class="inline-flex items-center gap-1 bg-surface-container-high '
                "hover:bg-primary-container/20 text-on-surface-variant "
                "hover:text-primary text-[9px] font-bold px-2 py-0.5 "
                'rounded-full uppercase tracking-wider transition-colors">'
                '<span class="material-symbols-outlined text-[10px]">folder</span>'
                f"{title}</a>"
            )
        else:
            case_badge = (
                '<span class="text-[9px] bg-amber-100 text-amber-700 px-2 py-0.5 '
                'rounded-full font-bold uppercase tracking-wider">Unlinked</span>'
            )

        sender_text = f"Via: {doc.sender}" if doc.sender else "Manual upload"

        card_html = (
            f'<div class="group relative rounded-lg border border-outline-variant/10 '
            "bg-surface-container-lowest hover:border-primary/20 "
            "hover:bg-surface-container transition-all duration-200 "
            f'cursor-pointer overflow-hidden" '
            f'style="border-left: 4px solid {stripe_color};" '
            f'hx-get="/document/{doc.id}" hx-target="#activity-doc-pane" '
            f'hx-swap="innerHTML" @click="activeDoc = \'{doc.id}\'">'
            '<div class="p-4 flex items-start gap-4">'
            f'<div class="shrink-0 w-10 h-10 rounded-lg flex items-center '
            f'justify-center" style="background-color: {stripe_color}20;">'
            f'<span class="material-symbols-outlined text-lg" '
            f'style="color: {stripe_color};">{stripe_icon}</span></div>'
            '<div class="flex-1 min-w-0">'
            '<div class="flex items-center justify-between gap-3 mb-1">'
            f'<h3 class="text-sm font-bold text-on-surface truncate '
            'group-hover:text-primary transition-colors">'
            f'{doc.title}</h3><span class="text-[10px] font-mono '
            'text-on-surface-variant shrink-0">'
            f"{doc.created_at.strftime('%Y-%m-%d %H:%M')}</span></div>"
            '<div class="flex items-center gap-2 flex-wrap">'
            f'{case_badge}<span class="text-[9px] text-on-surface-variant">'
            f"{sender_text}</span></div></div>"
            '<div class="shrink-0 opacity-0 group-hover:opacity-100 '
            'transition-opacity"><span class="material-symbols-outlined '
            'text-on-surface-variant">chevron_right</span>'
            "</div></div></div>"
        )
        html_parts.append(card_html)

    return HTMLResponse(content="".join(html_parts))


@router.get("/ingest")
async def ingest_status(request: Request, db: Session = Depends(get_db)):
    """Page showing document ingest queue status."""
    from app.models.database import IngestStatus

    pending_docs = (
        db.query(Document)
        .filter(
            Document.ingest_status.in_([IngestStatus.PENDING, IngestStatus.PROCESSING])
        )
        .order_by(Document.created_at.desc())
        .all()
    )

    recent_completed = (
        db.query(Document)
        .filter(
            Document.ingest_status == IngestStatus.COMPLETED,
            Document.ingest_completed_at.isnot(None),
        )
        .order_by(Document.ingest_completed_at.desc())
        .limit(20)
        .all()
    )

    recent_failed = (
        db.query(Document)
        .filter(Document.ingest_status == IngestStatus.FAILED)
        .order_by(Document.ingest_completed_at.desc())
        .limit(20)
        .all()
    )

    case_titles = {c.id: c.title for c in db.query(Case.id, Case.title).all()}

    return render_page(
        request,
        "pages/ingest_status.html",
        db=db,
        pending_docs=pending_docs,
        recent_completed=recent_completed,
        recent_failed=recent_failed,
        case_titles=case_titles,
    )


@router.get("/api/ingest-status")
async def api_ingest_status(
    request: Request,
    db: Session = Depends(get_db),
    limit: int = 20,
):
    """HTMX endpoint for ingest status updates."""
    from fastapi.responses import HTMLResponse

    from app.models.database import IngestStatus

    docs = (
        db.query(Document)
        .filter(
            Document.ingest_status.in_([IngestStatus.PENDING, IngestStatus.PROCESSING])
        )
        .order_by(Document.created_at.desc())
        .limit(limit)
        .all()
    )

    case_titles = {c.id: c.title for c in db.query(Case.id, Case.title).all()}

    html_parts = []
    for doc in docs:
        status_color = {
            IngestStatus.PENDING: "bg-yellow-500",
            IngestStatus.PROCESSING: "bg-blue-500",
        }.get(doc.ingest_status, "bg-gray-500")

        status_label = {
            IngestStatus.PENDING: "Pending",
            IngestStatus.PROCESSING: "Processing",
        }.get(doc.ingest_status, "Unknown")

        case_name = case_titles.get(doc.case_id, doc.case_id or "Unlinked")

        html = f"""
        <div class="flex items-center gap-3 p-3 rounded-lg bg-surface-container-low border border-outline-variant/10">
            <div class="w-2 h-2 rounded-full {status_color} animate-pulse"></div>
            <div class="flex-1 min-w-0">
                <p class="text-sm font-medium text-on-surface truncate">{doc.title}</p>
                <p class="text-[10px] text-on-surface-variant">{case_name}</p>
            </div>
            <span class="text-[10px] font-medium px-2 py-0.5 rounded bg-surface-container-high text-on-surface-variant">
                {status_label}
            </span>
        </div>
        """
        html_parts.append(html)

    return HTMLResponse(
        content="".join(html_parts)
        if html_parts
        else '<p class="text-sm text-on-surface-variant p-3">No pending documents</p>'
    )
