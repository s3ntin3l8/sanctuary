import os
from datetime import datetime
from typing import Optional
from fastapi import APIRouter, Depends, File, Form, HTTPException, Request, UploadFile
from fastapi.responses import HTMLResponse, RedirectResponse
from sqlalchemy.orm import Session

from app.constants import (
    ORIGINATOR_COLORS,
    ORIGINATOR_ICONS,
    COST_CATEGORY_META,
    COST_STATUS_META,
)
from app.dependencies import get_db
from app.helpers import (
    render_page,
    parse_form_datetime,
    render_case_schedule_panel,
    build_document_extraction_context,
    format_upcoming_datetime,
)
from app.models.database import (
    CostCategory,
    CostStatus,
    Deadline,
    Document,
    Hearing,
    LegalCost,
    OriginatorType,
)
from app.services.ingestion import (
    ingest_file,
    IngestionError,
    ALLOWED_EXTENSIONS,
    compute_review_reasons,
)
from app.services.ai_summary import summarize_document, trigger_summary_async

router = APIRouter()


@router.post("/triage/resolve/{doc_id}")
async def resolve_triage(doc_id: int, request: Request, db: Session = Depends(get_db)):
    doc = db.query(Document).filter(Document.id == doc_id).first()
    if not doc:
        return HTMLResponse("", status_code=404)

    doc.needs_review = False
    doc.review_reasons = []
    db.commit()
    db.refresh(doc)

    # Re-render the full review card with updated state
    from app.constants import ORIGINATOR_COLORS, ORIGINATOR_ICONS
    from collections import defaultdict

    top_level_docs = (
        db.query(Document)
        .filter(Document.case_id == (doc.case_id or ""), Document.parent_id == None)
        .order_by(Document.created_at.desc())
        .all()
    )
    resolved_docs = [d for d in top_level_docs if not d.needs_review]
    resolved_by_month = defaultdict(list)
    for d in resolved_docs:
        dt = d.created_at or d.received_date
        month_key = dt.strftime("%B %Y") if dt else "Unknown"
        resolved_by_month[month_key].append(d)

    def _month_sort_key(m):
        if m == "Unknown":
            return datetime.min
        try:
            return datetime.strptime(m, "%B %Y")
        except ValueError:
            return datetime.min

    resolved_by_month = dict(
        sorted(
            resolved_by_month.items(), key=lambda x: _month_sort_key(x[0]), reverse=True
        )
    )

    stripe_color = ORIGINATOR_COLORS.get(doc.originator_type, "#64748b")
    stripe_icon = ORIGINATOR_ICONS.get(doc.originator_type, "help_outline")

    return templates.TemplateResponse(
        "partials/review_card.html",
        {
            "request": request,
            "doc": doc,
            "stripe_color": stripe_color,
            "stripe_icon": stripe_icon,
            "top_level_docs": top_level_docs,
            "resolved_by_month": resolved_by_month,
            "originator_colors": ORIGINATOR_COLORS,
            "originator_icons": ORIGINATOR_ICONS,
        },
    )


@router.post("/document/{doc_id}/link-parent")
async def link_document_to_parent(
    doc_id: int,
    request: Request,
    db: Session = Depends(get_db),
):
    form = await request.form()
    parent_id = form.get("parent_id")

    if not parent_id:
        return HTMLResponse(
            '<span class="text-[9px] text-error">Select a parent document</span>',
            status_code=400,
        )

    try:
        parent_id = int(parent_id)
    except (ValueError, TypeError):
        return HTMLResponse(
            '<span class="text-[9px] text-error">Invalid parent ID</span>',
            status_code=400,
        )

    doc = db.query(Document).filter(Document.id == doc_id).first()
    if not doc:
        return HTMLResponse(
            '<span class="text-[9px] text-error">Document not found</span>',
            status_code=404,
        )

    parent = db.query(Document).filter(Document.id == parent_id).first()
    if not parent:
        return HTMLResponse(
            '<span class="text-[9px] text-error">Parent document not found</span>',
            status_code=404,
        )

    if doc_id == parent_id:
        return HTMLResponse(
            '<span class="text-[9px] text-error">Cannot link to itself</span>',
            status_code=400,
        )

    if doc.case_id and parent.case_id and doc.case_id != parent.case_id:
        return HTMLResponse(
            '<span class="text-[9px] text-error">Parent must be in the same case</span>',
            status_code=400,
        )

    if parent.parent_id == doc_id:
        return HTMLResponse(
            '<span class="text-[9px] text-error">Cannot create circular reference</span>',
            status_code=400,
        )

    doc.parent_id = parent_id

    if doc.review_reasons and "missing_parent" in doc.review_reasons:
        doc.review_reasons = [r for r in doc.review_reasons if r != "missing_parent"]
        if not doc.review_reasons:
            doc.needs_review = False

    db.commit()
    db.refresh(doc)

    # Re-render the full review card with updated state
    from app.constants import ORIGINATOR_COLORS, ORIGINATOR_ICONS
    from collections import defaultdict

    # Get resolved docs grouped by month for the picker
    top_level_docs = (
        db.query(Document)
        .filter(Document.case_id == (doc.case_id or ""), Document.parent_id == None)
        .order_by(Document.created_at.desc())
        .all()
    )
    resolved_docs = [d for d in top_level_docs if not d.needs_review]
    resolved_by_month = defaultdict(list)
    for d in resolved_docs:
        dt = d.created_at or d.received_date
        month_key = dt.strftime("%B %Y") if dt else "Unknown"
        resolved_by_month[month_key].append(d)

    def _month_sort_key(m):
        if m == "Unknown":
            return datetime.min
        try:
            return datetime.strptime(m, "%B %Y")
        except ValueError:
            return datetime.min

    resolved_by_month = dict(
        sorted(
            resolved_by_month.items(), key=lambda x: _month_sort_key(x[0]), reverse=True
        )
    )

    stripe_color = ORIGINATOR_COLORS.get(doc.originator_type, "#64748b")
    stripe_icon = ORIGINATOR_ICONS.get(doc.originator_type, "help_outline")

    return templates.TemplateResponse(
        "partials/review_card.html",
        {
            "request": request,
            "doc": doc,
            "stripe_color": stripe_color,
            "stripe_icon": stripe_icon,
            "top_level_docs": top_level_docs,
            "resolved_by_month": resolved_by_month,
            "originator_colors": ORIGINATOR_COLORS,
            "originator_icons": ORIGINATOR_ICONS,
        },
    )


@router.post("/document/{doc_id}/unlink-parent")
async def unlink_document_from_parent(
    doc_id: int,
    request: Request,
    db: Session = Depends(get_db),
):
    doc = db.query(Document).filter(Document.id == doc_id).first()
    if not doc:
        return HTMLResponse(
            '<span class="text-[9px] text-error">Document not found</span>',
            status_code=404,
        )

    doc.parent_id = None

    if not doc.review_reasons:
        doc.review_reasons = []
    if "missing_parent" not in doc.review_reasons:
        doc.review_reasons.append("missing_parent")
        doc.needs_review = True

    db.commit()
    db.refresh(doc)

    # Re-render the full review card with updated state
    from app.constants import ORIGINATOR_COLORS, ORIGINATOR_ICONS
    from collections import defaultdict

    top_level_docs = (
        db.query(Document)
        .filter(Document.case_id == (doc.case_id or ""), Document.parent_id == None)
        .order_by(Document.created_at.desc())
        .all()
    )
    resolved_docs = [d for d in top_level_docs if not d.needs_review]
    resolved_by_month = defaultdict(list)
    for d in resolved_docs:
        dt = d.created_at or d.received_date
        month_key = dt.strftime("%B %Y") if dt else "Unknown"
        resolved_by_month[month_key].append(d)

    def _month_sort_key(m):
        if m == "Unknown":
            return datetime.min
        try:
            return datetime.strptime(m, "%B %Y")
        except ValueError:
            return datetime.min

    resolved_by_month = dict(
        sorted(
            resolved_by_month.items(), key=lambda x: _month_sort_key(x[0]), reverse=True
        )
    )

    stripe_color = ORIGINATOR_COLORS.get(doc.originator_type, "#64748b")
    stripe_icon = ORIGINATOR_ICONS.get(doc.originator_type, "help_outline")

    return templates.TemplateResponse(
        "partials/review_card.html",
        {
            "request": request,
            "doc": doc,
            "stripe_color": stripe_color,
            "stripe_icon": stripe_icon,
            "top_level_docs": top_level_docs,
            "resolved_by_month": resolved_by_month,
            "originator_colors": ORIGINATOR_COLORS,
            "originator_icons": ORIGINATOR_ICONS,
        },
    )


@router.post("/costs")
async def create_cost(
    request: Request,
    db: Session = Depends(get_db),
):
    form = await request.form()
    errors = []

    case_id = (form.get("case_id") or "").strip()
    category = (form.get("category") or "").strip()
    title = (form.get("title") or "").strip()

    if not case_id:
        errors.append("Case is required")
    if not category:
        errors.append("Category is required")
    if not title:
        errors.append("Title is required")

    try:
        amount_net = float(form.get("amount_net", 0))
        if amount_net < 0:
            errors.append("Net amount cannot be negative")
    except (ValueError, TypeError):
        errors.append("Invalid net amount")
        amount_net = 0

    try:
        vat_rate = float(form.get("vat_rate", 0)) / 100
    except (ValueError, TypeError):
        vat_rate = 0

    try:
        amount_gross = float(form.get("amount_gross", 0))
    except (ValueError, TypeError):
        amount_gross = amount_net * (1 + vat_rate)

    if errors:
        error_list = "".join(f'<li class="text-xs text-error">{e}</li>' for e in errors)
        return HTMLResponse(
            f'<div id="cost-form-container" class="bg-surface-container rounded-2xl border border-error/30 p-6 shadow-sm">'
            f'<div class="flex items-center justify-between mb-4"><h3 class="text-sm font-black text-error">Failed to Add Cost</h3>'
            f"<button onclick=\"this.closest('#cost-form-container').innerHTML=''\" class=\"material-symbols-outlined text-on-surface-variant hover:text-error cursor-pointer text-sm\">close</button></div>"
            f'<ul class="space-y-1 mb-4">{error_list}</ul>'
            f'<button hx-get="/costs/new" hx-target="#cost-form-container" hx-swap="innerHTML" class="text-sm font-bold text-primary underline hover:no-underline cursor-pointer">Try Again</button>'
            f"</div>",
            status_code=400,
        )

    try:
        streitwert = float(form.get("streitwert")) if form.get("streitwert") else None
        gebuehren_faktor = (
            float(form.get("gebuehren_faktor"))
            if form.get("gebuehren_faktor")
            else None
        )
        due_at = parse_form_datetime(form.get("due_at"))

        cost = LegalCost(
            case_id=case_id,
            category=CostCategory(category),
            status=CostStatus.OFFEN,
            title=title,
            rvg_position=(form.get("rvg_position") or "").strip() or None,
            amount_net=amount_net,
            vat_rate=vat_rate,
            amount_gross=amount_gross,
            streitwert=streitwert,
            gebuehren_faktor=gebuehren_faktor,
            is_reimbursable=form.get("is_reimbursable") == "on",
            due_at=due_at,
            notes=(form.get("notes") or "").strip() or None,
        )
        db.add(cost)
        db.commit()
        db.refresh(cost)
    except Exception as e:
        db.rollback()
        return HTMLResponse(
            f'<div class="p-3 text-sm text-error">Failed to create cost: {e}</div>',
            status_code=400,
        )

    # Redirect back to costs page
    return RedirectResponse(url="/costs", status_code=303)


@router.post("/costs/{cost_id}/pay")
async def mark_cost_paid(
    request: Request,
    cost_id: int,
    db: Session = Depends(get_db),
):
    cost = db.get(LegalCost, cost_id)
    if not cost:
        return HTMLResponse(
            '<div class="text-red-500 text-xs">Cost not found</div>',
            status_code=404,
        )
    cost.amount_paid = cost.amount_gross
    cost.paid_at = datetime.now()
    cost.status = CostStatus.BEZAHLT
    db.commit()
    db.refresh(cost)
    return render_page(
        request,
        "partials/cost_row.html",
        db=db,
        cost=cost,
        cost_category_meta=COST_CATEGORY_META,
        cost_status_meta=COST_STATUS_META,
    )


@router.post("/costs/{cost_id}/reimburse")
async def mark_cost_reimbursed(
    request: Request,
    cost_id: int,
    db: Session = Depends(get_db),
):
    cost = db.get(LegalCost, cost_id)
    if not cost:
        return HTMLResponse(
            '<div class="text-red-500 text-xs">Cost not found</div>',
            status_code=404,
        )
    cost.amount_reimbursed = cost.amount_gross
    cost.status = CostStatus.ERSTATTET
    db.commit()
    db.refresh(cost)
    return render_page(
        request,
        "partials/cost_row.html",
        db=db,
        cost=cost,
        cost_category_meta=COST_CATEGORY_META,
        cost_status_meta=COST_STATUS_META,
    )


@router.post("/upload")
async def upload_document(
    request: Request,
    file: UploadFile = File(...),
    case_id: Optional[str] = Form(None),
    parent_id: Optional[int] = Form(None),
    db: Session = Depends(get_db),
):
    if file.filename:
        ext = os.path.splitext(file.filename)[1].lower()
        if ext not in ALLOWED_EXTENSIONS:
            if request.headers.get("hx-request"):
                return HTMLResponse(
                    f"<div class=\"p-3 text-sm text-error\">Unsupported file type '{ext}'. Allowed: {', '.join(sorted(ALLOWED_EXTENSIONS))}</div>",
                    status_code=400,
                )
            return {
                "error": f"Unsupported file type '{ext}'. Allowed: {', '.join(sorted(ALLOWED_EXTENSIONS))}"
            }, 400

    try:
        doc = await ingest_file(file, case_id, db, parent_id)
    except HTTPException as e:
        if request.headers.get("hx-request"):
            return HTMLResponse(
                f'<div class="p-3 text-sm text-error">{e.detail}</div>',
                status_code=e.status_code,
            )
        return {"error": e.detail}, e.status_code
    except IngestionError as e:
        if request.headers.get("hx-request"):
            return HTMLResponse(
                f'<div class="p-3 text-sm text-error">{e.message}</div>',
                status_code=500,
            )
        return {"error": e.message}, 500
    except Exception:
        if request.headers.get("hx-request"):
            return HTMLResponse(
                '<div class="p-3 text-sm text-error">An unexpected error occurred during upload.</div>',
                status_code=500,
            )
        return {"error": "An unexpected error occurred during upload."}, 500

    # Trigger AI summary (fire-and-forget)
    try:
        from app.services.ai_summary import trigger_summary_async

        trigger_summary_async(doc.id)
    except Exception:
        pass

    if doc.content and "Conversion failed:" in doc.content:
        warning_msg = f"File saved but conversion failed: {doc.content}"
        if request.headers.get("hx-request"):
            return HTMLResponse(
                f'<div class="p-3 text-sm text-warning">{warning_msg}</div>',
                status_code=200,
            )
        return {
            "message": warning_msg,
            "doc_id": doc.id,
            "case_id": doc.case_id,
            "parent_id": doc.parent_id,
            "title": doc.title,
        }

    if request.headers.get("hx-request"):
        return HTMLResponse(
            '<div class="p-3 text-sm text-on-surface-variant">File ingested successfully</div>',
        )
    return {
        "message": "File ingested successfully",
        "doc_id": doc.id,
        "case_id": doc.case_id,
        "parent_id": doc.parent_id,
        "title": doc.title,
    }


@router.post("/document/{doc_id}/promote/deadline")
async def promote_document_deadline(
    request: Request,
    doc_id: int,
    db: Session = Depends(get_db),
):
    form = await request.form()
    doc = db.query(Document).filter(Document.id == doc_id).first()
    if not doc:
        return HTMLResponse(
            '<div class="text-xs text-error">Document not found.</div>',
            status_code=404,
        )

    # If doc is in triage, reassign it to the target case
    target_case_id = (form.get("case_id") or "").strip() or None
    if doc.case_id == "_TRIAGE" and target_case_id:
        doc.case_id = target_case_id
        db.commit()

    if not doc.case_id or doc.case_id == "_TRIAGE":
        return HTMLResponse(
            '<div class="text-xs text-error">Document must be linked to a case first.</div>',
            status_code=400,
        )

    # If doc is in triage, reassign it to the target case
    target_case_id = (form.get("case_id") or "").strip() or None
    if doc.case_id == "_TRIAGE" and target_case_id:
        doc.case_id = target_case_id
        db.commit()

    if not doc.case_id or doc.case_id == "_TRIAGE":
        return HTMLResponse(
            '<div class="text-xs text-error">Document must be linked to a case first.</div>',
            status_code=400,
        )

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


@router.post("/document/{doc_id}/promote/hearing")
async def promote_document_hearing(
    request: Request,
    doc_id: int,
    db: Session = Depends(get_db),
):
    form = await request.form()
    doc = db.query(Document).filter(Document.id == doc_id).first()
    if not doc:
        return HTMLResponse(
            '<div class="text-xs text-error">Document not found.</div>',
            status_code=404,
        )

    # If doc is in triage, reassign it to the target case
    target_case_id = (form.get("case_id") or "").strip() or None
    if doc.case_id == "_TRIAGE" and target_case_id:
        doc.case_id = target_case_id
        db.commit()

    if not doc.case_id or doc.case_id == "_TRIAGE":
        return HTMLResponse(
            '<div class="text-xs text-error">Document must be linked to a case first.</div>',
            status_code=400,
        )

    # If doc is in triage, reassign it to the target case
    target_case_id = (form.get("case_id") or "").strip() or None
    if doc.case_id == "_TRIAGE" and target_case_id:
        doc.case_id = target_case_id
        db.commit()

    if not doc.case_id or doc.case_id == "_TRIAGE":
        return HTMLResponse(
            '<div class="text-xs text-error">Document must be linked to a case first.</div>',
            status_code=400,
        )

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


@router.post("/cases/{case_id}/deadlines")
async def create_case_deadline(
    request: Request,
    case_id: str,
    db: Session = Depends(get_db),
):
    form = await request.form()
    due_at = parse_form_datetime(form.get("due_at"))
    title = (form.get("title") or "").strip()

    errors = []
    if not title:
        errors.append("Title is required")
    if not due_at:
        errors.append("Valid date/time is required")

    if errors:
        return render_case_schedule_panel(
            request, db, case_id, deadline_errors=errors, deadline_data=dict(form)
        )

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


@router.post("/cases/{case_id}/deadlines/{deadline_id}")
async def update_case_deadline(
    request: Request,
    case_id: str,
    deadline_id: int,
    db: Session = Depends(get_db),
):
    form = await request.form()
    deadline = (
        db.query(Deadline)
        .filter(Deadline.id == deadline_id, Deadline.case_id == case_id)
        .first()
    )
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


@router.post("/cases/{case_id}/hearings")
async def create_case_hearing(
    request: Request,
    case_id: str,
    db: Session = Depends(get_db),
):
    form = await request.form()
    scheduled_for = parse_form_datetime(form.get("scheduled_for"))
    title = (form.get("title") or "").strip()

    errors = []
    if not title:
        errors.append("Title is required")
    if not scheduled_for:
        errors.append("Valid date/time is required")

    if errors:
        return render_case_schedule_panel(
            request, db, case_id, hearing_errors=errors, hearing_data=dict(form)
        )

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


@router.post("/cases/{case_id}/hearings/{hearing_id}")
async def update_case_hearing(
    request: Request,
    case_id: str,
    hearing_id: int,
    db: Session = Depends(get_db),
):
    form = await request.form()
    hearing = (
        db.query(Hearing)
        .filter(Hearing.id == hearing_id, Hearing.case_id == case_id)
        .first()
    )
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


@router.post("/document/{doc_id}/summarize")
async def regenerate_summary(
    request: Request,
    doc_id: int,
    db: Session = Depends(get_db),
):
    doc = await summarize_document(doc_id, db)
    extraction_context = build_document_extraction_context(db, doc)
    return render_page(
        request,
        "partials/document_details.html",
        db=db,
        doc_id=doc_id,
        doc=doc,
        format_upcoming_datetime=format_upcoming_datetime,
        **extraction_context,
    )


async def render_document_extraction_panel(request: Request, doc_id: int, db: Session):
    from app.helpers import format_form_datetime, format_upcoming_datetime

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
