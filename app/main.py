from contextlib import asynccontextmanager
from datetime import datetime, timedelta
from typing import Generator, Optional
from fastapi import FastAPI, Request, Depends, UploadFile, File, Form
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates

from app.models.database import (
    Base,
    engine,
    SessionLocal,
    Case,
    CaseStatus,
    Deadline,
    Document,
    Hearing,
    OriginatorType,
)
from sqlalchemy.orm import Session
from fastapi.responses import HTMLResponse

# Seed data for known cases
_SEED_CASES = [
    {"id": "ADV-992-K", "title": "Vane vs. Vane: Divorce & Assets",       "court_id": "2024-FL-DR-00992", "status": CaseStatus.DISCOVERY},
    {"id": "ADV-804-M", "title": "Smith Construction vs. City Council",    "court_id": "2024-CV-00804",    "status": CaseStatus.PRE_TRIAL},
    {"id": "REF-441-22","title": "Mercury Tech IP Dispute",                "court_id": "2022-IP-HC-00441", "status": CaseStatus.CLOSED},
]

_SEED_DEADLINES = [
    {
        "case_id": "ADV-992-K",
        "title": "File supplemental financial disclosure",
        "description": "Updated asset schedule requested before the next conference.",
        "offset_days": 3,
    },
    {
        "case_id": "ADV-804-M",
        "title": "Respond to interrogatories",
        "description": "Serve final discovery responses on opposing counsel.",
        "offset_days": 6,
    },
    {
        "case_id": "ADV-804-M",
        "title": "Submit witness exhibit list",
        "description": "Court requires pre-trial exhibit exchange before motion hearing.",
        "offset_days": 11,
    },
]

_SEED_HEARINGS = [
    {
        "case_id": "ADV-992-K",
        "title": "Settlement conference",
        "description": "Case management conference with both parties present.",
        "location": "Superior Court, Room 4B",
        "offset_days": 5,
        "hour": 9,
        "minute": 30,
    },
    {
        "case_id": "ADV-804-M",
        "title": "Pre-trial motions hearing",
        "description": "Argument on municipal records and expert disclosure motions.",
        "location": "County Courthouse, Courtroom 12",
        "offset_days": 9,
        "hour": 14,
        "minute": 0,
    },
]

@asynccontextmanager
async def lifespan(app: FastAPI):
    Base.metadata.create_all(bind=engine)
    # Seed cases that don't exist yet
    db: Session = SessionLocal()
    try:
        for seed in _SEED_CASES:
            if not db.get(Case, seed["id"]):
                db.add(Case(**seed))
        db.commit()

        if db.query(Deadline).count() == 0:
            now = datetime.utcnow().replace(second=0, microsecond=0)
            for seed in _SEED_DEADLINES:
                db.add(
                    Deadline(
                        case_id=seed["case_id"],
                        title=seed["title"],
                        description=seed["description"],
                        due_at=now + timedelta(days=seed["offset_days"]),
                    )
                )

        if db.query(Hearing).count() == 0:
            base_time = datetime.utcnow().replace(second=0, microsecond=0)
            for seed in _SEED_HEARINGS:
                scheduled_day = base_time + timedelta(days=seed["offset_days"])
                db.add(
                    Hearing(
                        case_id=seed["case_id"],
                        title=seed["title"],
                        description=seed["description"],
                        location=seed["location"],
                        scheduled_for=scheduled_day.replace(
                            hour=seed["hour"],
                            minute=seed["minute"],
                        ),
                    )
                )

        db.commit()
    finally:
        db.close()
    yield

app = FastAPI(title="The Sanctuary", lifespan=lifespan)

# DB Dependency
def get_db() -> Generator:
    db = SessionLocal()
    try:
        yield db
    finally:
        db.close()


app.mount("/static", StaticFiles(directory="static"), name="static")

templates = Jinja2Templates(directory="app/templates")

# ---------------------------------------------------------------------------
# Shared template context helpers
# ---------------------------------------------------------------------------
def build_sidebar_counts(db: Session) -> dict:
    """Computes sidebar badge counts using the active request session."""
    triage_count = db.query(Document).filter(Document.needs_review == True).count()
    total_docs = db.query(Document).count()
    case_count = db.query(Case).filter(Case.status != CaseStatus.CLOSED).count() or len(_SEED_CASES)
    return {
        "triage_count": triage_count,
        "total_docs": total_docs,
        "case_count": case_count,
    }


def render_page(
    request: Request,
    template_name: str,
    db: Optional[Session] = None,
    **context,
):
    base_context = {"request": request}
    if db is not None:
        base_context["sidebar_counts"] = build_sidebar_counts(db)
    base_context.update(context)
    return templates.TemplateResponse(template_name, base_context)


def format_relative_time(value: datetime) -> str:
    """Returns a compact human-readable relative timestamp."""
    delta = datetime.utcnow() - value
    total_seconds = max(int(delta.total_seconds()), 0)
    if total_seconds < 60:
        return "just now"
    if total_seconds < 3600:
        minutes = total_seconds // 60
        return f"{minutes}m ago"
    if total_seconds < 86400:
        hours = total_seconds // 3600
        return f"{hours}h ago"
    days = total_seconds // 86400
    if days == 1:
        return "yesterday"
    if days < 7:
        return f"{days}d ago"
    return value.strftime("%b %d, %Y")


def format_upcoming_datetime(value: datetime) -> str:
    """Formats upcoming deadlines/hearings for compact dashboard display."""
    delta_days = (value.date() - datetime.utcnow().date()).days
    if delta_days == 0:
        day_label = "Today"
    elif delta_days == 1:
        day_label = "Tomorrow"
    else:
        day_label = value.strftime("%a, %b %d")
    return f"{day_label} at {value.strftime('%H:%M')}"


def format_deadline_badge(value: datetime) -> dict:
    """Returns a compact urgency label + tone for dashboard deadline cards."""
    day_delta = (value.date() - datetime.utcnow().date()).days
    if day_delta < 0:
        return {"label": "Overdue", "tone": "bg-error-container/30 text-error"}
    if day_delta == 0:
        return {"label": "Today", "tone": "bg-error-container/30 text-error"}
    if day_delta == 1:
        return {"label": "1 day left", "tone": "bg-originator-opposing/10 text-originator-opposing"}
    if day_delta < 7:
        return {"label": f"{day_delta} days left", "tone": "bg-originator-opposing/10 text-originator-opposing"}
    return {"label": value.strftime("%b %d"), "tone": "bg-surface-container-high text-on-surface-variant"}


def format_form_datetime(value: Optional[datetime]) -> str:
    """Formats datetimes for datetime-local form fields."""
    if value is None:
        return ""
    return value.strftime("%Y-%m-%dT%H:%M")


def parse_form_datetime(raw_value: Optional[str]) -> Optional[datetime]:
    """Parses datetime-local input values, tolerating blanks."""
    if not raw_value:
        return None
    try:
        return datetime.fromisoformat(raw_value)
    except ValueError:
        return None


def load_case_schedule(db: Session, case_id: str) -> dict:
    """Loads schedule data for the case calendar panel."""
    now = datetime.utcnow()
    deadlines = (
        db.query(Deadline)
        .filter(Deadline.case_id == case_id)
        .order_by(Deadline.completed.asc(), Deadline.due_at.asc())
        .all()
    )
    hearings = (
        db.query(Hearing)
        .filter(Hearing.case_id == case_id)
        .order_by(Hearing.scheduled_for.asc())
        .all()
    )
    return {
        "upcoming_deadlines": [item for item in deadlines if not item.completed and item.due_at >= now],
        "completed_deadlines": [item for item in deadlines if item.completed or item.due_at < now],
        "upcoming_hearings": [item for item in hearings if item.scheduled_for >= now],
        "past_hearings": [item for item in hearings if item.scheduled_for < now],
    }


def render_case_schedule_panel(request: Request, db: Session, case_id: str):
    schedule = load_case_schedule(db, case_id)
    return render_page(
        request,
        "partials/case_schedule_panel.html",
        case_id=case_id,
        format_upcoming_datetime=format_upcoming_datetime,
        format_form_datetime=format_form_datetime,
        **schedule,
    )


def build_document_extraction_context(db: Session, doc: Optional[Document]) -> dict:
    """Builds extracted schedule candidates and already-promoted records for a document."""
    if not doc:
        return {
            "schedule_candidates": [],
            "linked_deadlines": [],
            "linked_hearings": [],
        }

    from app.services.ingestion import extract_schedule_candidates

    schedule_candidates = extract_schedule_candidates(doc.content or "", base_date=doc.received_date)
    linked_deadlines = (
        db.query(Deadline)
        .filter(Deadline.source_document_id == doc.id)
        .order_by(Deadline.due_at.asc())
        .all()
    )
    linked_hearings = (
        db.query(Hearing)
        .filter(Hearing.source_document_id == doc.id)
        .order_by(Hearing.scheduled_for.asc())
        .all()
    )
    return {
        "schedule_candidates": schedule_candidates,
        "linked_deadlines": linked_deadlines,
        "linked_hearings": linked_hearings,
    }

# ---------------------------------------------------------------------------
# Review field definitions — what the ingestion pipeline checks
# ---------------------------------------------------------------------------
REVIEW_FIELD_LABELS = {
    "missing_case_id":        {"label": "Case ID",            "icon": "folder",          "field": "case_id"},
    "missing_originator":     {"label": "Originator Type",    "icon": "person",          "field": "originator_type"},
    "missing_sender":         {"label": "Sender / Source",    "icon": "mail",            "field": "sender"},
    "missing_received_date":  {"label": "Received Date",      "icon": "calendar_today",  "field": "received_date"},
    "missing_parent":         {"label": "Parent Relationship","icon": "account_tree",    "field": "parent_id"},
    "missing_title":          {"label": "Document Title",     "icon": "title",           "field": "title"},
    "missing_content":        {"label": "Document Content",   "icon": "article",         "field": "content"},
}

templates.env.globals["review_field_labels"] = REVIEW_FIELD_LABELS

@app.get("/")
async def dashboard(request: Request, db: Session = Depends(get_db)):
    week_ago = datetime.utcnow() - timedelta(days=7)
    now = datetime.utcnow()

    all_cases = db.query(Case).order_by(Case.created_at.desc()).all()
    case_titles = {case.id: case.title for case in all_cases}

    active_cases = [case for case in all_cases if case.status != CaseStatus.CLOSED]
    active_case_count = len(active_cases)
    new_active_cases_this_week = sum(1 for case in active_cases if case.created_at >= week_ago)

    pending_docs = (
        db.query(Document)
        .filter(Document.needs_review == True)
        .order_by(Document.created_at.desc())
        .all()
    )
    pending_review_count = len(pending_docs)
    pending_added_this_week = sum(1 for doc in pending_docs if doc.created_at >= week_ago)

    court_doc_count = db.query(Document).filter(Document.originator_type == OriginatorType.COURT).count()
    new_documents_this_week = db.query(Document).filter(Document.created_at >= week_ago).count()

    priority_docs = pending_docs[:4]
    recent_documents = (
        db.query(Document)
        .order_by(Document.created_at.desc())
        .limit(4)
        .all()
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
        case_titles=case_titles,
        originator_colors=ORIGINATOR_COLORS,
        originator_icons=ORIGINATOR_ICONS,
        status_meta=CASE_STATUS_META,
        format_relative_time=format_relative_time,
        format_upcoming_datetime=format_upcoming_datetime,
        format_deadline_badge=format_deadline_badge,
    )



# Originator stripe colors from GEMINI.md §4
ORIGINATOR_COLORS = {
    OriginatorType.COURT: "#0369A1",
    OriginatorType.OPPOSING: "#B91C1C",
    OriginatorType.OWN: "#047857",
    OriginatorType.UNKNOWN: "#64748b",
}

ORIGINATOR_ICONS = {
    OriginatorType.COURT: "gavel",
    OriginatorType.OPPOSING: "warning",
    OriginatorType.OWN: "shield",
    OriginatorType.UNKNOWN: "help_outline",
}
# Status display metadata for the template
CASE_STATUS_META = {
    CaseStatus.INTAKE:     {"label": "Intake",      "color": "bg-slate-100 text-slate-700",  "dot": "bg-slate-400"},
    CaseStatus.DISCOVERY:  {"label": "Discovery",   "color": "bg-blue-50 text-blue-700",     "dot": "bg-blue-500"},
    CaseStatus.PRE_TRIAL:  {"label": "Pre-Trial",   "color": "bg-amber-50 text-amber-700",   "dot": "bg-amber-500"},
    CaseStatus.TRIAL:      {"label": "Trial",       "color": "bg-rose-50 text-rose-700",     "dot": "bg-rose-500"},
    CaseStatus.POST_TRIAL: {"label": "Post-Trial",  "color": "bg-purple-50 text-purple-700", "dot": "bg-purple-500"},
    CaseStatus.CLOSED:     {"label": "Closed",      "color": "bg-slate-100 text-slate-500",  "dot": "bg-slate-300"},
}

@app.get("/cases")
async def case_directory(request: Request, db: Session = Depends(get_db)):
    all_cases = db.query(Case).order_by(Case.created_at.desc()).all()

    # Auto-create Case rows for any orphaned document case_ids not yet in the table
    doc_case_ids = {r[0] for r in db.query(Document.case_id).filter(Document.case_id != None).distinct().all()}
    existing_ids = {c.id for c in all_cases}
    for cid in doc_case_ids - existing_ids:
        new_case = Case(id=cid, title=f"Case {cid}", status=CaseStatus.INTAKE)
        db.add(new_case)
        all_cases.append(new_case)
    if doc_case_ids - existing_ids:
        db.commit()

    active_cases  = [c for c in all_cases if c.status != CaseStatus.CLOSED]
    closed_cases  = [c for c in all_cases if c.status == CaseStatus.CLOSED]

    return render_page(
        request,
        "pages/case_directory.html",
        db=db,
        active_cases=active_cases,
        closed_cases=closed_cases,
        status_meta=CASE_STATUS_META,
    )

@app.get("/cases/{case_id}")
async def case_stream(request: Request, case_id: str, db: Session = Depends(get_db)):
    # Documents needing review for THIS case (shown in the "Needs Review" banner)
    review_docs = db.query(Document).filter(
        Document.case_id == case_id,
        Document.needs_review == True
    ).order_by(Document.created_at.desc()).all()

    # All top-level documents for the chronology (resolved ones)
    chrono_docs = db.query(Document).filter(
        Document.case_id == case_id,
        Document.parent_id == None,
        Document.needs_review == False
    ).order_by(Document.created_at.desc()).all()

    case = db.get(Case, case_id)
    case_title = case.title if case else f"Case {case_id}"
    court_id = case.court_id if case and case.court_id else ""
    schedule = load_case_schedule(db, case_id)

    return render_page(
        request,
        "pages/case_stream.html",
        db=db,
        review_docs=review_docs,
        documents=chrono_docs,
        case_id=case_id,
        case_title=case_title,
        court_id=court_id,
        upcoming_deadlines=schedule["upcoming_deadlines"],
        completed_deadlines=schedule["completed_deadlines"],
        upcoming_hearings=schedule["upcoming_hearings"],
        past_hearings=schedule["past_hearings"],
        originator_colors=ORIGINATOR_COLORS,
        originator_icons=ORIGINATOR_ICONS,
        format_upcoming_datetime=format_upcoming_datetime,
        format_form_datetime=format_form_datetime,
    )

@app.get("/triage")
async def triage_center(request: Request, db: Session = Depends(get_db)):
    documents = db.query(Document).filter(Document.needs_review == True).order_by(Document.created_at.desc()).all()
    return render_page(
        request,
        "pages/triage.html",
        db=db,
        documents=documents,
        originator_colors=ORIGINATOR_COLORS,
        originator_icons=ORIGINATOR_ICONS,
    )

@app.post("/triage/resolve/{doc_id}")
async def resolve_triage(doc_id: int, db: Session = Depends(get_db)):
    doc = db.query(Document).filter(Document.id == doc_id).first()
    if doc:
        doc.needs_review = False
        doc.review_reasons = []
        db.commit()
    # Return an empty fragment so HTMX removes the card from the UI
    from fastapi.responses import HTMLResponse
    return HTMLResponse(
        '<div class="hidden" data-resolved="true"></div>'
    )

@app.post("/triage/update/{doc_id}")
async def update_triage_field(
    request: Request,
    doc_id: int,
    db: Session = Depends(get_db),
):
    """Accept field updates from the triage review form."""
    form = await request.form()
    doc = db.query(Document).filter(Document.id == doc_id).first()
    if not doc:
        from fastapi.responses import HTMLResponse
        return HTMLResponse('<div class="text-red-500 text-xs">Document not found</div>', status_code=404)

    # Apply each submitted field
    updatable = {"case_id", "originator_type", "sender", "received_date", "title", "parent_id"}
    for key in form.keys():
        if key in updatable:
            val = form[key]
            if key == "originator_type" and val:
                val = OriginatorType(val)
            elif key == "received_date" and val:
                from datetime import datetime as dt
                try:
                    val = dt.fromisoformat(val)
                except ValueError:
                    continue
            elif key == "parent_id" and val:
                try:
                    val = int(val)
                except ValueError:
                    continue
            if val == "":
                val = None
            setattr(doc, key, val)

    # Recompute review reasons
    from app.services.ingestion import compute_review_reasons
    reasons = compute_review_reasons(doc)
    doc.review_reasons = reasons
    doc.needs_review = len(reasons) > 0
    db.commit()
    db.refresh(doc)

    # Return updated triage card (HTMX swap)
    if doc.needs_review:
        return render_page(
            request,
            "partials/triage_card.html",
            doc=doc,
            stripe_color=ORIGINATOR_COLORS.get(doc.originator_type, '#64748b'),
            stripe_icon=ORIGINATOR_ICONS.get(doc.originator_type, 'help_outline'),
        )
    else:
        from fastapi.responses import HTMLResponse
        return HTMLResponse('<div class="hidden" data-resolved="true"></div>')

@app.get("/timeline")
async def master_timeline(request: Request, db: Session = Depends(get_db)):
    from itertools import groupby
    # All top-level documents across every case, newest first
    all_docs = (
        db.query(Document)
        .filter(Document.parent_id == None)
        .order_by(Document.created_at.desc())
        .all()
    )

    # Group by "Month Year" (e.g. "October 2023")
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

@app.get("/costs")
async def legal_costs(request: Request, db: Session = Depends(get_db)):
    return render_page(request, "pages/costs.html", db=db)

@app.get("/contacts")
async def contacts(request: Request, db: Session = Depends(get_db)):
    return render_page(request, "pages/contacts.html", db=db)

@app.get("/document/{doc_id}")
async def get_document_details(request: Request, doc_id: str, db: Session = Depends(get_db)):
    # Retrieve the document securely
    doc = db.query(Document).filter(Document.id == doc_id).first()
    # Retrieve the partial for the HTMX request
    extraction_context = build_document_extraction_context(db, doc)
    return render_page(
        request,
        "partials/document_details.html",
        doc_id=doc_id,
        doc=doc,
        format_upcoming_datetime=format_upcoming_datetime,
        **extraction_context,
    )

from app.services.ingestion import ingest_file

@app.post("/upload")
async def upload_document(
    file: UploadFile = File(...),
    case_id: Optional[str] = Form(None),
    parent_id: int = Form(None),
    db = Depends(get_db)
):
    doc = await ingest_file(file, case_id, db, parent_id)
    return {"message": "File ingested successfully", "doc_id": doc.id, "case_id": doc.case_id, "parent_id": doc.parent_id, "title": doc.title}


@app.post("/document/{doc_id}/promote/deadline")
async def promote_document_deadline(
    request: Request,
    doc_id: int,
    db: Session = Depends(get_db),
):
    form = await request.form()
    doc = db.query(Document).filter(Document.id == doc_id).first()
    if not doc or not doc.case_id:
        return HTMLResponse('<div class="text-xs text-error">Document must be linked to a case first.</div>', status_code=400)

    due_at = parse_form_datetime(form.get("due_at"))
    title = (form.get("title") or "").strip()
    description = (form.get("description") or "").strip() or None
    if title and due_at:
        existing = (
            db.query(Deadline)
            .filter(
                Deadline.case_id == doc.case_id,
                Deadline.source_document_id == doc.id,
                Deadline.title == title,
                Deadline.due_at == due_at,
            )
            .first()
        )
        if not existing:
            db.add(
                Deadline(
                    case_id=doc.case_id,
                    title=title,
                    description=description,
                    due_at=due_at,
                    source_document_id=doc.id,
                )
            )
            db.commit()

    return await render_document_extraction_panel(request, doc_id, db)


@app.post("/document/{doc_id}/promote/hearing")
async def promote_document_hearing(
    request: Request,
    doc_id: int,
    db: Session = Depends(get_db),
):
    form = await request.form()
    doc = db.query(Document).filter(Document.id == doc_id).first()
    if not doc or not doc.case_id:
        return HTMLResponse('<div class="text-xs text-error">Document must be linked to a case first.</div>', status_code=400)

    scheduled_for = parse_form_datetime(form.get("scheduled_for"))
    title = (form.get("title") or "").strip()
    description = (form.get("description") or "").strip() or None
    if title and scheduled_for:
        existing = (
            db.query(Hearing)
            .filter(
                Hearing.case_id == doc.case_id,
                Hearing.source_document_id == doc.id,
                Hearing.title == title,
                Hearing.scheduled_for == scheduled_for,
            )
            .first()
        )
        if not existing:
            db.add(
                Hearing(
                    case_id=doc.case_id,
                    title=title,
                    description=description,
                    scheduled_for=scheduled_for,
                    source_document_id=doc.id,
                )
            )
            db.commit()

    return await render_document_extraction_panel(request, doc_id, db)


@app.get("/document/{doc_id}/extractions")
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
        doc=doc,
        format_upcoming_datetime=format_upcoming_datetime,
        format_form_datetime=format_form_datetime,
        **extraction_context,
    )


@app.post("/cases/{case_id}/deadlines")
async def create_case_deadline(
    request: Request,
    case_id: str,
    db: Session = Depends(get_db),
):
    form = await request.form()
    due_at = parse_form_datetime(form.get("due_at"))
    title = (form.get("title") or "").strip()

    if title and due_at:
        db.add(
            Deadline(
                case_id=case_id,
                title=title,
                description=(form.get("description") or "").strip() or None,
                due_at=due_at,
            )
        )
        db.commit()

    return render_case_schedule_panel(request, db, case_id)


@app.post("/cases/{case_id}/deadlines/{deadline_id}")
async def update_case_deadline(
    request: Request,
    case_id: str,
    deadline_id: int,
    db: Session = Depends(get_db),
):
    form = await request.form()
    deadline = db.query(Deadline).filter(Deadline.id == deadline_id, Deadline.case_id == case_id).first()
    if deadline:
        title = (form.get("title") or "").strip()
        due_at = parse_form_datetime(form.get("due_at"))
        deadline.title = title or deadline.title
        if due_at:
            deadline.due_at = due_at
        deadline.description = (form.get("description") or "").strip() or None
        deadline.completed = form.get("completed") == "on"
        db.commit()

    return render_case_schedule_panel(request, db, case_id)


@app.post("/cases/{case_id}/hearings")
async def create_case_hearing(
    request: Request,
    case_id: str,
    db: Session = Depends(get_db),
):
    form = await request.form()
    scheduled_for = parse_form_datetime(form.get("scheduled_for"))
    title = (form.get("title") or "").strip()

    if title and scheduled_for:
        db.add(
            Hearing(
                case_id=case_id,
                title=title,
                description=(form.get("description") or "").strip() or None,
                location=(form.get("location") or "").strip() or None,
                scheduled_for=scheduled_for,
            )
        )
        db.commit()

    return render_case_schedule_panel(request, db, case_id)


@app.post("/cases/{case_id}/hearings/{hearing_id}")
async def update_case_hearing(
    request: Request,
    case_id: str,
    hearing_id: int,
    db: Session = Depends(get_db),
):
    form = await request.form()
    hearing = db.query(Hearing).filter(Hearing.id == hearing_id, Hearing.case_id == case_id).first()
    if hearing:
        title = (form.get("title") or "").strip()
        scheduled_for = parse_form_datetime(form.get("scheduled_for"))
        hearing.title = title or hearing.title
        if scheduled_for:
            hearing.scheduled_for = scheduled_for
        hearing.location = (form.get("location") or "").strip() or None
        hearing.description = (form.get("description") or "").strip() or None
        db.commit()

    return render_case_schedule_panel(request, db, case_id)
