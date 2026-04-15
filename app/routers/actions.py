import contextlib
import os
from datetime import UTC, datetime

from fastapi import (
    APIRouter,
    BackgroundTasks,
    Depends,
    Form,
    HTTPException,
    Request,
)
from fastapi.responses import HTMLResponse, RedirectResponse
from slowapi import Limiter
from slowapi.util import get_remote_address
from sqlalchemy.orm import Session

from app.config import OLLAMA_BASE_URL, templates

limiter = Limiter(key_func=get_remote_address)
from app.constants import (
    COST_CATEGORY_META,
    COST_STATUS_META,
    ORIGINATOR_COLORS,
    ORIGINATOR_ICONS,
)
from app.dependencies import get_db
from app.helpers import (
    build_document_extraction_context,
    format_upcoming_datetime,
    parse_form_datetime,
    render_case_schedule_panel,
    render_page,
)
from app.models.database import (
    Case,
    CostCategory,
    CostStatus,
    Deadline,
    Document,
    Hearing,
    IngestStatus,
    LegalCost,
    OriginatorType,
)
from app.services.ai_summary import (
    check_ollama_status,
    summarize_document,
)
from app.services.embeddings import check_embedding_status
from app.services.ingestion import (
    ALLOWED_EXTENSIONS,
    compute_review_reasons,
    ingest_file,
)

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
    from collections import defaultdict

    top_level_docs = (
        db.query(Document)
        .filter(Document.case_id == (doc.case_id or ""), Document.parent_id is None)
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
        request,
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


@router.post("/document/{doc_id}/update-triage")
async def update_triage_document(
    doc_id: int, request: Request, db: Session = Depends(get_db)
):
    from app.constants import REVIEW_FIELD_LABELS
    from app.helpers import build_document_extraction_context, format_form_datetime

    doc = db.query(Document).filter(Document.id == doc_id).first()
    if not doc:
        return HTMLResponse("Document not found", status_code=404)

    form = await request.form()

    # Update fields
    if "title" in form:
        doc.title = form.get("title").strip()
    if "case_id" in form:
        case_id = form.get("case_id")
        doc.case_id = case_id if case_id else None
    if "sender" in form:
        doc.sender = form.get("sender").strip()
    if "originator_type" in form:
        with contextlib.suppress(ValueError):
            doc.originator_type = OriginatorType(form.get("originator_type"))
    if "received_date" in form:
        date_str = form.get("received_date")
        if date_str:
            with contextlib.suppress(ValueError):
                doc.received_date = datetime.fromisoformat(date_str)

    # Manual resolve toggle
    if form.get("mark_resolved") == "true":
        doc.needs_review = False
        doc.review_reasons = []
    else:
        # Re-evaluate reasons
        doc.review_reasons = compute_review_reasons(doc, db)
        doc.needs_review = len(doc.review_reasons) > 0

    db.commit()
    db.refresh(doc)

    # Response prep
    cases = db.query(Case).filter(Case.id != "_TRIAGE").order_by(Case.title.asc()).all()
    extraction_context = build_document_extraction_context(db, doc)
    stripe_color = ORIGINATOR_COLORS.get(doc.originator_type, "#64748b")
    stripe_icon = ORIGINATOR_ICONS.get(doc.originator_type, "help_outline")

    # If resolved, send an OOB swap to delete the card from the triage list
    oob_card = ""
    if not doc.needs_review:
        oob_card = f'<div id="triage-wrapper-{doc.id}" hx-swap-oob="delete"></div>'
    else:
        # Otherwise, update the triage card in the list
        oob_card = templates.get_template("partials/triage_card.html").render(
            {
                "request": request,
                "doc": doc,
                "stripe_color": stripe_color,
                "stripe_icon": stripe_icon,
                "review_field_labels": REVIEW_FIELD_LABELS,
                "activeDoc": str(doc.id),
                "hx_swap_oob": "true",
            }
        )

    main_response = templates.get_template("partials/document_details.html").render(
        {
            "request": request,
            "doc": doc,
            "doc_id": doc.id,
            "cases": cases,
            "OriginatorType": OriginatorType,
            "review_field_labels": REVIEW_FIELD_LABELS,
            "format_upcoming_datetime": format_upcoming_datetime,
            "format_form_datetime": format_form_datetime,
            **extraction_context,
        }
    )

    return HTMLResponse(content=main_response + oob_card)


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
    from collections import defaultdict

    # Get resolved docs grouped by month for the picker
    top_level_docs = (
        db.query(Document)
        .filter(Document.case_id == (doc.case_id or ""), Document.parent_id is None)
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

    stripe_color = ORIGINATOR_COLORS.get(doc.originator_type, "#64748b")
    stripe_icon = ORIGINATOR_ICONS.get(doc.originator_type, "help_outline")

    return templates.TemplateResponse(
        request,
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
    from collections import defaultdict

    top_level_docs = (
        db.query(Document)
        .filter(Document.case_id == (doc.case_id or ""), Document.parent_id is None)
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

    stripe_color = ORIGINATOR_COLORS.get(doc.originator_type, "#64748b")
    stripe_icon = ORIGINATOR_ICONS.get(doc.originator_type, "help_outline")

    return templates.TemplateResponse(
        request,
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


@router.post("/costs/{cost_id}/update-field")
async def update_cost_field(
    request: Request,
    cost_id: int,
    field: str = Form(...),
    value: str = Form(...),
    db: Session = Depends(get_db),
):
    cost = db.get(LegalCost, cost_id)
    if not cost:
        return HTMLResponse(
            '<div class="text-red-500 text-xs">Cost not found</div>',
            status_code=404,
        )

    try:
        if field == "amount_gross":
            cost.amount_gross = float(value.replace(",", "."))
        elif field == "amount_paid":
            cost.amount_paid = float(value.replace(",", "."))
        elif field == "amount_reimbursed":
            cost.amount_reimbursed = float(value.replace(",", "."))
        elif field == "streitwert":
            cost.streitwert = float(value.replace(",", ".")) if value else None
        else:
            return HTMLResponse(
                f'<div class="text-red-500 text-xs">Unknown field: {field}</div>',
                status_code=400,
            )
        db.commit()
        db.refresh(cost)
    except ValueError:
        return HTMLResponse(
            '<div class="text-red-500 text-xs">Invalid number format</div>',
            status_code=400,
        )

    return render_page(
        request,
        "partials/cost_row.html",
        db=db,
        cost=cost,
        cost_category_meta=COST_CATEGORY_META,
        cost_status_meta=COST_STATUS_META,
    )


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
    cost.paid_at = datetime.now(UTC)
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
    background_tasks: BackgroundTasks,
    db: Session = Depends(get_db),
):
    form = await request.form()
    files = form.getlist("files")
    case_id_raw = form.get("case_id")
    case_id = case_id_raw if case_id_raw else None
    parent_id_raw = form.get("parent_id")
    parent_id = int(parent_id_raw) if parent_id_raw else None

    if not files or all(not f.filename for f in files):
        if request.headers.get("hx-request"):
            return HTMLResponse(
                '<div class="p-3 text-sm text-error">No files selected.</div>',
                status_code=400,
            )
        return {"error": "No files selected."}, 400

    results = []
    success_count = 0
    error_count = 0

    for file in files:
        if not file.filename:
            continue

        ext = os.path.splitext(file.filename)[1].lower()
        if ext not in ALLOWED_EXTENSIONS:
            error_count += 1
            err_msg = (
                f'<div class="p-2 text-xs text-error">'
                f"'{file.filename}': Unsupported file type '{ext}'</div>"
            )
            results.append(err_msg)
            continue

        try:
            doc = await ingest_file(file, case_id, db, parent_id, skip_processing=True)
            success_count += 1

            try:
                background_tasks.add_task(process_document_background, doc.id, db)
            except Exception as e:
                print(f"Background task error: {e}")

            results.append(
                f'<div class="p-2 text-xs text-green-400">✓ {file.filename} queued for processing</div>'
            )

        except HTTPException as e:
            error_count += 1
            results.append(
                f'<div class="p-2 text-xs text-error">'
                f"✗ {file.filename}: {e.detail}</div>"
            )
        except Exception as e:
            import traceback

            error_count += 1
            results.append(
                f'<div class="p-2 text-xs text-error">'
                f"✗ {file.filename}: Upload failed: {e}</div>"
            )
            traceback.print_exc()

    if success_count == 0 and error_count > 0:
        return HTMLResponse(
            f"<div class='space-y-1'>{''.join(results)}</div>",
            status_code=400,
        )

    if request.headers.get("hx-request"):
        summary = f"<div class='p-2 text-xs font-bold text-on-surface'>{success_count} uploaded, {error_count} failed</div>"
        return HTMLResponse(
            f"<div class='space-y-1'>{summary}{''.join(results)}</div>",
            status_code=200 if success_count > 0 else 400,
        )

    return {
        "success": success_count,
        "errors": error_count,
        "results": results,
    }, 200 if success_count > 0 else 400


def process_document_background(doc_id: int, db: Session):
    """Background task to process document after upload."""
    from datetime import UTC, datetime

    from app.services.ingestion import process_uploaded_document

    doc = db.query(Document).filter(Document.id == doc_id).first()
    if not doc:
        return

    doc.ingest_status = IngestStatus.PROCESSING
    doc.ingest_started_at = datetime.now(UTC)
    db.commit()

    try:
        process_uploaded_document(doc, db)
        doc.ingest_status = IngestStatus.COMPLETED
    except Exception as e:
        doc.ingest_status = IngestStatus.FAILED
        doc.ingest_error = str(e)

    doc.ingest_completed_at = datetime.now(UTC)
    db.commit()


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
            '<div class="text-xs text-error">'
            "Document must be linked to a case first.</div>",
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
            '<div class="text-xs text-error">'
            "Document must be linked to a case first.</div>",
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


@router.post("/document/{doc_id}/approve-summary")
async def approve_summary(
    request: Request,
    doc_id: int,
    action: str = "approve",
    db: Session = Depends(get_db),
):
    """Approve or reject an AI summary. Action: 'approve' or 'reject'."""
    from datetime import datetime

    doc = db.query(Document).filter(Document.id == doc_id).first()
    if not doc:
        raise HTTPException(status_code=404, detail="Document not found")

    if action == "approve":
        doc.ai_summary_status = "approved"
        doc.ai_summary_approved_at = datetime.now(UTC)
    elif action == "reject":
        doc.ai_summary_status = "pending"
        doc.ai_summary_approved_at = None
    else:
        raise HTTPException(
            status_code=400, detail="Invalid action. Use 'approve' or 'reject'"
        )

    db.commit()

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


@router.post("/document/{doc_id}/reingest")
async def reingest_document(
    request: Request,
    doc_id: int,
    db: Session = Depends(get_db),
):
    """Re-run extraction on an existing document."""
    doc = db.query(Document).filter(Document.id == doc_id).first()
    if not doc:
        raise HTTPException(status_code=404, detail="Document not found")

    if not doc.file_path or not os.path.exists(doc.file_path):
        raise HTTPException(status_code=400, detail="Original file not found")

    from app.services.ingestion import (
        extract_case_id,
        extract_cost_candidates,
        extract_legal_categories,
        extract_originator,
        extract_received_date,
        extract_sender,
    )

    try:
        with open(doc.file_path, "rb") as f:
            content = f.read()
    except Exception as e:
        raise HTTPException(status_code=500, detail="Cannot read original file") from e

    markdown_content = content.decode("utf-8", errors="ignore")

    (case_id, case_id_conf) = extract_case_id(doc.title, markdown_content)
    (originator_type, originator_conf) = extract_originator(doc.title, markdown_content)
    (received_date, date_conf) = extract_received_date(markdown_content, doc.title)
    (sender, sender_conf) = extract_sender(markdown_content)
    cost_candidates = extract_cost_candidates(markdown_content)
    extract_legal_categories(markdown_content)

    doc.originator_type = originator_type
    doc.sender = sender
    doc.received_date = received_date
    doc.cost_candidates = cost_candidates if cost_candidates else None

    doc.extraction_confidence = {
        "sender": sender_conf,
        "date": date_conf,
        "case_id": case_id_conf,
        "originator": originator_conf,
    }

    doc.ai_summary_status = "pending"
    doc.ai_summary = None
    doc.ai_summary_created_at = None

    db.commit()

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


@router.post("/cases/{case_id}/reingest-all")
async def reingest_case_documents(
    request: Request,
    case_id: str,
    db: Session = Depends(get_db),
):
    """Re-run extraction on all documents in a case."""
    docs = db.query(Document).filter(Document.case_id == case_id).all()
    if not docs:
        raise HTTPException(status_code=404, detail="No documents found for case")

    from app.services.ingestion import (
        extract_case_id,
        extract_cost_candidates,
        extract_originator,
        extract_received_date,
        extract_sender,
    )

    reingested_count = 0
    for doc in docs:
        if not doc.file_path or not os.path.exists(doc.file_path):
            continue

        try:
            with open(doc.file_path, "rb") as f:
                content = f.read()
            markdown_content = content.decode("utf-8", errors="ignore")

            (case_id, case_id_conf) = extract_case_id(doc.title, markdown_content)
            (originator_type, originator_conf) = extract_originator(
                doc.title, markdown_content
            )
            (received_date, date_conf) = extract_received_date(
                markdown_content, doc.title
            )
            (sender, sender_conf) = extract_sender(markdown_content)
            cost_candidates = extract_cost_candidates(markdown_content)

            doc.originator_type = originator_type
            doc.sender = sender
            doc.received_date = received_date
            doc.cost_candidates = cost_candidates if cost_candidates else None

            doc.extraction_confidence = {
                "sender": sender_conf,
                "date": date_conf,
                "case_id": case_id_conf,
                "originator": originator_conf,
            }

            doc.ai_summary_status = "pending"
            reingested_count += 1
        except Exception:
            continue

    db.commit()

    msg = (
        f"<div class='p-3 text-sm text-on-surface'>"
        f"Re-ingested {reingested_count} documents</div>"
    )
    return HTMLResponse(msg)


@router.get("/api/ai-status")
async def get_ai_status(request: Request):
    """API endpoint for AI status check."""
    status_summary = await check_ollama_status()
    embed_status = await check_embedding_status()

    # Merge status
    status = {
        "reachable": status_summary["reachable"],
        "summary_model": status_summary["summary_model"],
        "embedding_model": embed_status["embedding_model"],
        "error": status_summary["error"] or embed_status["error"],
    }

    return templates.TemplateResponse(
        request,
        "partials/ai_status.html",
        {"request": request, "status": status, "base_url": OLLAMA_BASE_URL},
    )
