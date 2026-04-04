from contextlib import asynccontextmanager
from typing import Generator, Optional
from fastapi import FastAPI, Request, Depends, UploadFile, File, Form
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates

from app.models.database import Base, engine, SessionLocal, Case, CaseStatus, Document, OriginatorType
from sqlalchemy.orm import Session

# Seed data for known cases
_SEED_CASES = [
    {"id": "ADV-992-K", "title": "Vane vs. Vane: Divorce & Assets",       "court_id": "2024-FL-DR-00992", "status": CaseStatus.DISCOVERY},
    {"id": "ADV-804-M", "title": "Smith Construction vs. City Council",    "court_id": "2024-CV-00804",    "status": CaseStatus.PRE_TRIAL},
    {"id": "REF-441-22","title": "Mercury Tech IP Dispute",                "court_id": "2022-IP-HC-00441", "status": CaseStatus.CLOSED},
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
# Sidebar counts — injected into every template via Jinja2 global function
# ---------------------------------------------------------------------------
def _sidebar_counts() -> dict:
    """Returns live counts for sidebar badges. Called once per render."""
    db = SessionLocal()
    try:
        triage_count = db.query(Document).filter(Document.needs_review == True).count()
        total_docs = db.query(Document).count()
        case_count = db.query(Case).filter(Case.status != CaseStatus.CLOSED).count() or len(_SEED_CASES)
        return {
            "triage_count": triage_count,
            "total_docs": total_docs,
            "case_count": case_count,
        }
    finally:
        db.close()

templates.env.globals["sidebar_counts"] = _sidebar_counts

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
    return templates.TemplateResponse("pages/dashboard.html", {"request": request})



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


def get_case_title(case_id: str) -> str:
    db = SessionLocal()
    try:
        case = db.get(Case, case_id)
        return case.title if case else f"Case {case_id}"
    finally:
        db.close()

def get_court_id(case_id: str) -> str:
    db = SessionLocal()
    try:
        case = db.get(Case, case_id)
        return case.court_id or "" if case else ""
    finally:
        db.close()

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

    return templates.TemplateResponse("pages/case_directory.html", {
        "request": request,
        "active_cases": active_cases,
        "closed_cases": closed_cases,
        "status_meta": CASE_STATUS_META,
    })

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

    case_title = get_case_title(case_id)
    court_id = get_court_id(case_id)
    return templates.TemplateResponse("pages/case_stream.html", {
        "request": request,
        "review_docs": review_docs,
        "documents": chrono_docs,
        "case_id": case_id,
        "case_title": case_title,
        "court_id": court_id,
        "originator_colors": ORIGINATOR_COLORS,
        "originator_icons": ORIGINATOR_ICONS,
    })

@app.get("/triage")
async def triage_center(request: Request, db: Session = Depends(get_db)):
    documents = db.query(Document).filter(Document.needs_review == True).order_by(Document.created_at.desc()).all()
    return templates.TemplateResponse("pages/triage.html", {
        "request": request,
        "documents": documents,
        "originator_colors": ORIGINATOR_COLORS,
        "originator_icons": ORIGINATOR_ICONS,
    })

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
        return templates.TemplateResponse("partials/triage_card.html", {
            "request": request,
            "doc": doc,
            "stripe_color": ORIGINATOR_COLORS.get(doc.originator_type, '#64748b'),
            "stripe_icon": ORIGINATOR_ICONS.get(doc.originator_type, 'help_outline'),
        })
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

    return templates.TemplateResponse("pages/timeline.html", {
        "request": request,
        "grouped_docs": grouped,
        "total_docs": total_docs,
        "pending_count": pending_count,
        "case_titles": cases,
        "originator_colors": ORIGINATOR_COLORS,
        "originator_icons": ORIGINATOR_ICONS,
    })

@app.get("/costs")
async def legal_costs(request: Request):
    return templates.TemplateResponse("pages/costs.html", {"request": request})

@app.get("/contacts")
async def contacts(request: Request):
    return templates.TemplateResponse("pages/contacts.html", {"request": request})

@app.get("/document/{doc_id}")
async def get_document_details(request: Request, doc_id: str, db: Session = Depends(get_db)):
    # Retrieve the document securely
    doc = db.query(Document).filter(Document.id == doc_id).first()
    # Retrieve the partial for the HTMX request
    return templates.TemplateResponse("partials/document_details.html", {"request": request, "doc_id": doc_id, "doc": doc})

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
