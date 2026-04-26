from datetime import datetime, timedelta

from fastapi import APIRouter, Depends, Form, HTTPException, Request
from sqlalchemy.orm import Session

from app.constants import (
    CASE_STATUS_META,
    COST_CATEGORY_META,
    COST_STATUS_META,
)
from app.dependencies import get_db
from app.helpers import render_page
from app.models.database import Case, LegalCost
from app.models.enums import CostCategory, CostStatus
from app.services.case_service import recompute_total_cost_exposure
from app.services.cost_service import CostService

router = APIRouter(prefix="/costs", tags=["pages"])


@router.get("")
async def costs_page(request: Request, db: Session = Depends(get_db)):
    cost_service = CostService(db)
    data = cost_service.get_costs_for_page()

    case_titles = {c.id: c.title for c in db.query(Case.id, Case.title).all()}

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


@router.get("/cases/{case_id}/new")
async def new_cost_for_case(
    request: Request, case_id: str, db: Session = Depends(get_db)
):
    case = db.query(Case).filter(Case.id == case_id).first()
    all_cases = db.query(Case).order_by(Case.title.asc()).all()
    return render_page(
        request,
        "pages/cost_form.html",
        db=db,
        case=case,
        all_cases=all_cases,
        cost_category_meta=COST_CATEGORY_META,
        cost_status_meta=COST_STATUS_META,
    )


@router.post("")
async def create_cost(
    case_id: str = Form(...),
    category: CostCategory = Form(...),
    title: str = Form(...),
    amount_net: float = Form(...),
    vat_rate: float = Form(0.0),
    amount_gross: float | None = Form(None),
    status: CostStatus = Form(CostStatus.OFFEN),
    issued_at: str | None = Form(None),
    due_at: str | None = Form(None),
    proceeding_id: int | None = Form(None),
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
        issued_at=issued_date,
        due_at=due_date,
        proceeding_id=proceeding_id,
    )

    recompute_total_cost_exposure(case_id, db)

    # Return the new row for HTMX swap
    return render_page(
        None,
        "partials/cost_row.html",
        cost=cost,
        cost_status_meta=COST_STATUS_META,
        cost_category_meta=COST_CATEGORY_META,
    )


@router.post("/{cost_id}/pay")
async def mark_cost_paid(cost_id: int, db: Session = Depends(get_db)):
    cost_service = CostService(db)
    cost = cost_service.mark_as_paid(cost_id)
    if not cost:
        raise HTTPException(status_code=404, detail="Cost not found")

    recompute_total_cost_exposure(cost.case_id, db)

    return render_page(
        None,
        "partials/cost_row.html",
        cost=cost,
        cost_status_meta=COST_STATUS_META,
        cost_category_meta=COST_CATEGORY_META,
    )


@router.post("/{cost_id}/reimburse")
async def mark_cost_reimbursed(
    cost_id: int, amount: float | None = Form(None), db: Session = Depends(get_db)
):
    cost_service = CostService(db)
    target_cost = db.get(LegalCost, cost_id)
    if not target_cost:
        raise HTTPException(status_code=404, detail="Cost not found")

    reimburse_amount = amount if amount is not None else target_cost.amount_gross
    cost = cost_service.mark_as_reimbursed(cost_id, reimburse_amount)

    recompute_total_cost_exposure(cost.case_id, db)

    return render_page(
        None,
        "partials/cost_row.html",
        cost=cost,
        cost_status_meta=COST_STATUS_META,
        cost_category_meta=COST_CATEGORY_META,
    )


@router.post("/{cost_id}/update-field")
async def update_cost_field(
    cost_id: int,
    field: str = Form(...),
    value: str = Form(...),
    db: Session = Depends(get_db),
):
    cost = db.get(LegalCost, cost_id)
    if not cost:
        raise HTTPException(status_code=404, detail="Cost not found")

    if field == "title":
        cost.title = value
    elif field == "status":
        cost.status = CostStatus(value)
    elif field == "category":
        cost.category = CostCategory(value)
    elif field == "amount_net":
        cost.amount_net = float(value)
        cost.amount_gross = cost.amount_net * (1 + (cost.vat_rate or 0))
    elif field == "vat_rate":
        cost.vat_rate = float(value)
        cost.amount_gross = (cost.amount_net or 0) * (1 + cost.vat_rate)

    db.commit()
    db.refresh(cost)

    recompute_total_cost_exposure(cost.case_id, db)

    return render_page(
        None,
        "partials/cost_row.html",
        cost=cost,
        cost_status_meta=COST_STATUS_META,
        cost_category_meta=COST_CATEGORY_META,
    )
