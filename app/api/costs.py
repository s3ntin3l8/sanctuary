from fastapi import APIRouter, Depends, Request
from sqlalchemy.orm import Session

from app.constants import (
    CASE_STATUS_META,
    COST_CATEGORY_META,
    COST_STATUS_META,
)
from app.dependencies import get_db
from app.helpers import render_page
from app.models.database import Case
from app.services.cost_service import CostService

router = APIRouter(prefix="/costs", tags=["pages"])


@router.get("")
async def costs_page(request: Request, db: Session = Depends(get_db)):
    cost_service = CostService(db)
    data = cost_service.get_costs_for_page()

    case_titles = {c.id: c.title for c in db.query(Case.id, Case.title).all()}

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


@router.get("/cases/{case_id:path}/costs/new")
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
