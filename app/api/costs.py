import math
from datetime import datetime, timedelta

from fastapi import APIRouter, Depends, Form, HTTPException, Request
from fastapi.responses import HTMLResponse
from sqlalchemy.orm import Session

from app.config import templates
from app.constants import (
    CASE_STATUS_META,
    COST_CATEGORY_META,
    COST_STATUS_META,
)
from app.dependencies import get_db
from app.helpers import build_cost_summary, render_page
from app.models.database import Case, CostSignal, LegalCost
from app.models.enums import CaseStatus, CostCategory, CostStatus
from app.services.case_service import (
    build_case_level_costs,
    build_proceeding_exposure,
    recompute_total_cost_exposure,
)
from app.services.cost_service import CostService

router = APIRouter(prefix="/costs", tags=["pages"])


def _build_financials_oob_html(request: Request, case_id: str, db: Session) -> str:
    """Render the financial KPI strip and per-instance projection as OOB swaps.

    HTMX picks up the elements (marked `hx-swap-oob="true"`) and replaces
    `#financials-kpi` and `#financials-per-instance` in the page, so the
    headline numbers and the proceeding breakdown stay in lockstep with the
    triggering mutation without a full reload.
    """
    case_costs = db.query(LegalCost).filter(LegalCost.case_id == case_id).all()
    cost_summary = build_cost_summary(case_costs, CostStatus)
    case = db.query(Case).filter(Case.id == case_id).first()
    financials = {
        "total_cost_exposure": case.total_cost_exposure if case else 0,
        "proceeding_exposure": build_proceeding_exposure(case_id, db),
        "case_level_costs": build_case_level_costs(case_id, db),
    }
    kpi_html = templates.get_template("partials/dashboard/financials_kpi.html").render(
        request=request,
        cost_summary=cost_summary,
        financials=financials,
        oob=True,
    )
    per_instance_html = templates.get_template(
        "partials/dashboard/financials_per_instance.html"
    ).render(
        request=request,
        financials=financials,
        cost_status_meta=COST_STATUS_META,
        cost_category_meta=COST_CATEGORY_META,
        oob=True,
    )
    return kpi_html + per_instance_html


def _render_cost_row_with_kpi_oob(
    request: Request,
    cost: LegalCost,
    db: Session,
    row_style: str = "standard",
) -> HTMLResponse:
    """Updated cost row + OOB financials refresh.

    ``row_style`` decides which row template the main response carries:
      * ``"standard"`` — the 7-col ``partials/cost_row.html`` used by the
        global ``/costs`` page (with inline editors).
      * ``"bucket"`` — the 6-col ``partials/dashboard/cost_bucket_row.html``
        used inside the consolidated per-instance table on the case
        Financials view.
    The OOB tail (KPI strip + per-instance projection) is identical for
    both — only the targeted row HTML differs so the swap fits the
    surrounding table layout.
    """
    if row_style == "bucket":
        row_template = "partials/dashboard/cost_bucket_row.html"
    else:
        row_template = "partials/cost_row.html"
    row_html = templates.get_template(row_template).render(
        request=request,
        cost=cost,
        cost_status_meta=COST_STATUS_META,
        cost_category_meta=COST_CATEGORY_META,
    )
    oob_html = _build_financials_oob_html(request, cost.case_id, db)
    return HTMLResponse(row_html + oob_html)


def _parse_positive_float(value: str, field_name: str) -> float:
    """Parse a money amount; reject NaN, ±Inf, and non-positive values."""
    try:
        amount = float(value)
    except (TypeError, ValueError):
        raise HTTPException(status_code=422, detail=f"Invalid {field_name}") from None
    if not math.isfinite(amount) or amount <= 0:
        raise HTTPException(
            status_code=422, detail=f"{field_name} must be a positive finite number"
        )
    return amount


def _parse_vat_rate(value: str) -> float:
    """Parse a VAT rate (0.0–1.0); reject NaN, ±Inf, negatives, > 1.0."""
    try:
        rate = float(value)
    except (TypeError, ValueError):
        raise HTTPException(status_code=422, detail="Invalid vat_rate") from None
    if not math.isfinite(rate) or rate < 0 or rate > 1:
        raise HTTPException(status_code=422, detail="vat_rate must be between 0 and 1")
    return rate


@router.get("")
async def costs_page(request: Request, db: Session = Depends(get_db)):
    cost_service = CostService(db)
    data = cost_service.get_costs_for_page()

    # Dropdown only needs cases the user is likely to assign new costs to.
    # Closed cases still render their existing rows (joined elsewhere by case_id).
    case_titles = {
        c.id: c.title
        for c in db.query(Case.id, Case.title)
        .filter(Case.status != CaseStatus.CLOSED)
        .all()
    }

    # Compute overdue and upcoming costs for the alerts
    now = datetime.now()
    pending = cost_service.get_pending_costs()
    overdue_costs = [c for c in pending if c.due_at and c.due_at < now]
    upcoming_costs = [
        c for c in pending if c.due_at and now <= c.due_at < now + timedelta(days=7)
    ]

    return render_page(
        request,
        "pages/costs.html",
        db=db,
        all_costs=data["all_costs"],
        costs_by_case=data["costs_by_case"],
        global_summary=data["global_summary"],
        case_titles=case_titles,
        status_meta=CASE_STATUS_META,
        cost_status_meta=COST_STATUS_META,
        cost_category_meta=COST_CATEGORY_META,
        overdue_costs=overdue_costs,
        upcoming_costs=upcoming_costs,
    )


@router.get("/new")
async def new_cost_page(request: Request, db: Session = Depends(get_db)):
    all_cases = db.query(Case).order_by(Case.title.asc()).all()
    return render_page(
        request,
        "pages/cost_form.html",
        db=db,
        all_cases=all_cases,
        cost_category_meta=COST_CATEGORY_META,
        cost_status_meta=COST_STATUS_META,
    )


@router.post("")
async def create_cost(
    request: Request,
    case_id: str = Form(...),
    category: CostCategory = Form(...),
    title: str = Form(...),
    amount_net: float = Form(..., gt=0),
    vat_rate: float = Form(0.0, ge=0, le=1),
    amount_gross: float | None = Form(None, gt=0),
    status: CostStatus = Form(CostStatus.OFFEN),
    issued_at: str | None = Form(None),
    due_at: str | None = Form(None),
    proceeding_id: int | None = Form(None),
    rvg_position: str | None = Form(None),
    streitwert: float | None = Form(None),
    gebuehren_faktor: float | None = Form(None),
    notes: str | None = Form(None),
    is_reimbursable: bool = Form(True),
    row_style: str = Form("standard"),
    db: Session = Depends(get_db),
):
    if amount_gross is None:
        amount_gross = amount_net * (1 + vat_rate)

    issued_date = datetime.fromisoformat(issued_at) if issued_at else None
    due_date = datetime.fromisoformat(due_at) if due_at else None

    cost_service = CostService(db)
    cost = cost_service.create_cost(
        case_id=case_id,
        category=category,
        title=title,
        amount_net=amount_net,
        amount_gross=amount_gross,
        status=status,
        vat_rate=vat_rate,
        issued_at=issued_date,
        due_at=due_date,
        proceeding_id=proceeding_id,
        rvg_position=rvg_position,
        streitwert=streitwert,
        gebuehren_faktor=gebuehren_faktor,
        notes=notes,
        is_reimbursable=is_reimbursable,
    )

    recompute_total_cost_exposure(case_id, db)

    return _render_cost_row_with_kpi_oob(request, cost, db, row_style=row_style)


@router.post("/{cost_id}/pay")
async def mark_cost_paid(
    request: Request,
    cost_id: int,
    row_style: str = Form("standard"),
    db: Session = Depends(get_db),
):
    cost_service = CostService(db)
    cost = cost_service.mark_as_paid(cost_id)
    if not cost:
        raise HTTPException(status_code=404, detail="Cost not found")

    recompute_total_cost_exposure(cost.case_id, db)

    return _render_cost_row_with_kpi_oob(request, cost, db, row_style=row_style)


@router.post("/{cost_id}/reimburse")
async def mark_cost_reimbursed(
    request: Request,
    cost_id: int,
    amount: float | None = Form(None),
    row_style: str = Form("standard"),
    db: Session = Depends(get_db),
):
    cost_service = CostService(db)
    target_cost = db.get(LegalCost, cost_id)
    if not target_cost:
        raise HTTPException(status_code=404, detail="Cost not found")

    reimburse_amount = amount if amount is not None else target_cost.amount_gross
    cost = cost_service.mark_as_reimbursed(cost_id, reimburse_amount)
    if not cost:
        raise HTTPException(status_code=404, detail="Cost not found")

    recompute_total_cost_exposure(cost.case_id, db)

    return _render_cost_row_with_kpi_oob(request, cost, db, row_style=row_style)


@router.post("/{cost_id}/unpay")
async def mark_cost_unpaid(
    request: Request,
    cost_id: int,
    row_style: str = Form("standard"),
    db: Session = Depends(get_db),
):
    cost_service = CostService(db)
    cost = cost_service.mark_as_unpaid(cost_id)
    if not cost:
        raise HTTPException(status_code=404, detail="Cost not found")

    recompute_total_cost_exposure(cost.case_id, db)

    return _render_cost_row_with_kpi_oob(request, cost, db, row_style=row_style)


@router.post("/{cost_id}/unreimburse")
async def mark_cost_unreimbursed(
    request: Request,
    cost_id: int,
    row_style: str = Form("standard"),
    db: Session = Depends(get_db),
):
    cost_service = CostService(db)
    cost = cost_service.mark_as_unreimbursed(cost_id)
    if not cost:
        raise HTTPException(status_code=404, detail="Cost not found")

    recompute_total_cost_exposure(cost.case_id, db)

    return _render_cost_row_with_kpi_oob(request, cost, db, row_style=row_style)


@router.post("/{cost_id}/update-field")
async def update_cost_field(
    request: Request,
    cost_id: int,
    field: str = Form(...),
    value: str = Form(...),
    row_style: str = Form("standard"),
    db: Session = Depends(get_db),
):
    cost = db.get(LegalCost, cost_id)
    if not cost:
        raise HTTPException(status_code=404, detail="Cost not found")

    from app.services.cost_service import _derive_status

    if field == "title":
        cost.title = value
    elif field == "status":
        cost.status = CostStatus(value)
    elif field == "category":
        cost.category = CostCategory(value)
    elif field == "amount_net":
        amount = _parse_positive_float(value, "amount_net")
        cost.amount_net = amount
        cost.amount_gross = amount * (1 + (cost.vat_rate or 0))
    elif field == "vat_rate":
        rate = _parse_vat_rate(value)
        cost.vat_rate = rate
        cost.amount_gross = (cost.amount_net or 0) * (1 + rate)
    elif field == "amount_paid":
        cost.amount_paid = _parse_positive_float(value, "amount_paid")
        _derive_status(cost)
    elif field == "amount_reimbursed":
        cost.amount_reimbursed = _parse_positive_float(value, "amount_reimbursed")
        _derive_status(cost)
    elif field == "paid_at":
        cost.paid_at = datetime.fromisoformat(value) if value else None
    elif field == "due_at":
        cost.due_at = datetime.fromisoformat(value) if value else None
    elif field == "streitwert":
        cost.streitwert = float(value) if value else None
    elif field == "gebuehren_faktor":
        cost.gebuehren_faktor = float(value) if value else None
    elif field == "notes":
        cost.notes = value or None
    elif field == "is_reimbursable":
        cost.is_reimbursable = value.lower() in {"true", "1", "yes"}
    elif field == "offsets_cost_id":
        cost.offsets_cost_id = int(value) if value else None
    else:
        raise HTTPException(status_code=422, detail=f"Unknown field: {field}")

    db.commit()
    db.refresh(cost)

    recompute_total_cost_exposure(cost.case_id, db)

    return _render_cost_row_with_kpi_oob(request, cost, db, row_style=row_style)


@router.post("/signals/{signal_id}/auto-detect-role")
async def auto_detect_cost_signal_role(
    request: Request, signal_id: int, db: Session = Depends(get_db)
):
    """Re-side a cost-ruling signal by reading its source document via LLM.

    Wraps ``cost_ruling_sider.detect_cost_ruling_role``: when the model
    returns a confident winner/loser/shared/each_own verdict we persist the
    new allocation and refresh the page; on ``unknown`` or any internal
    failure we leave the existing allocation alone and surface the cell
    untouched (the user can still flip via the dropdown).
    """
    from app.services.intelligence.cost_ruling_sider import detect_cost_ruling_role

    signal = db.get(CostSignal, signal_id)
    if not signal:
        raise HTTPException(status_code=404, detail="Cost signal not found")
    if signal.signal_type.value != "cost_ruling":
        raise HTTPException(status_code=422, detail="Signal is not a cost ruling")

    new_alloc = detect_cost_ruling_role(signal_id, db)
    if new_alloc is not None:
        signal.allocation = new_alloc
        db.commit()
        db.refresh(signal)
        if signal.case_id:
            recompute_total_cost_exposure(signal.case_id, db)

    cell_html = templates.get_template("partials/cost_ruling_cell.html").render(
        request=request, signal=signal
    )
    oob_html = (
        _build_financials_oob_html(request, signal.case_id, db)
        if signal.case_id
        else ""
    )
    return HTMLResponse(cell_html + oob_html)


@router.post("/signals/{signal_id}/client-role")
async def update_cost_signal_client_role(
    request: Request,
    signal_id: int,
    role: str = Form(...),
    db: Session = Depends(get_db),
):
    """Side a cost-ruling signal from the client's perspective.

    Accepts ``winner`` / ``loser`` / ``unset`` and mutates the existing
    ``CostSignal.allocation`` JSON in place. Triggers a recompute so the
    headline projection and per-instance breakdown reflect who actually
    bears the costs.
    """
    if role not in {"winner", "loser", "unset"}:
        raise HTTPException(status_code=422, detail="Invalid client_role")

    signal = db.get(CostSignal, signal_id)
    if not signal:
        raise HTTPException(status_code=404, detail="Cost signal not found")

    allocation = dict(signal.allocation or {})
    if role == "unset":
        allocation.pop("client_role", None)
    else:
        allocation["client_role"] = role
    # A manual choice supersedes any prior auto-detect, so drop the markers.
    allocation.pop("auto_detected", None)
    allocation.pop("rationale", None)
    signal.allocation = allocation
    # SQLAlchemy doesn't dirty-track in-place JSON mutations; force the column
    # to be considered changed by reassigning the attribute.
    db.commit()
    db.refresh(signal)

    if signal.case_id:
        recompute_total_cost_exposure(signal.case_id, db)

    cell_html = templates.get_template("partials/cost_ruling_cell.html").render(
        request=request, signal=signal
    )
    oob_html = (
        _build_financials_oob_html(request, signal.case_id, db)
        if signal.case_id
        else ""
    )
    return HTMLResponse(cell_html + oob_html)
