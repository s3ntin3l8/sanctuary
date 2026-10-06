"""Cost rows and cost-signal roles as the case dashboard (and, later, the
costs page) mutate them. Each call returns the fresh row; the client
refetches the case financials for the derived totals."""

from __future__ import annotations

from fastapi import APIRouter, Depends, Response
from sqlalchemy.orm import Session

from app.api.access_guards import require_cost_access, require_cost_signal_access
from app.api.v1.case_detail import _cost_row
from app.api.v1.errors import ApiError
from app.dependencies import get_db
from app.models.database import CostSignal, LegalCost
from app.schemas.case_detail import ClientRoleUpdate, CostFieldUpdate, CostRow
from app.services.case_service import recompute_total_cost_exposure
from app.services.cost_service import CostService, _derive_status

router = APIRouter(tags=["costs"])


def _refreshed(db: Session, cost: LegalCost) -> CostRow:
    db.commit()
    db.refresh(cost)
    recompute_total_cost_exposure(cost.case_id, db)
    return _cost_row(cost)


@router.post("/costs/{cost_id}/pay", response_model=CostRow)
def mark_paid(
    db: Session = Depends(get_db),
    cost: LegalCost = Depends(require_cost_access(edit=True)),
):
    CostService(db).mark_as_paid(cost.id)
    return _refreshed(db, cost)


@router.post("/costs/{cost_id}/unpay", response_model=CostRow)
def mark_unpaid(
    db: Session = Depends(get_db),
    cost: LegalCost = Depends(require_cost_access(edit=True)),
):
    CostService(db).mark_as_unpaid(cost.id)
    return _refreshed(db, cost)


@router.post("/costs/{cost_id}/reimburse", response_model=CostRow)
def mark_reimbursed(
    db: Session = Depends(get_db),
    cost: LegalCost = Depends(require_cost_access(edit=True)),
):
    CostService(db).mark_as_reimbursed(cost.id, cost.amount_gross)
    return _refreshed(db, cost)


@router.post("/costs/{cost_id}/unreimburse", response_model=CostRow)
def mark_unreimbursed(
    db: Session = Depends(get_db),
    cost: LegalCost = Depends(require_cost_access(edit=True)),
):
    CostService(db).mark_as_unreimbursed(cost.id)
    return _refreshed(db, cost)


@router.patch("/costs/{cost_id}", response_model=CostRow)
def update_cost(
    body: CostFieldUpdate,
    db: Session = Depends(get_db),
    cost: LegalCost = Depends(require_cost_access(edit=True)),
):
    data = body.model_dump(exclude_none=True)
    if not data:
        raise ApiError(422, "empty_update", "Nothing to update.")
    if "title" in data:
        cost.title = data["title"].strip() or cost.title
    if "status" in data:
        cost.status = data["status"]
    if "category" in data:
        cost.category = data["category"]
    if "amount_net" in data:
        cost.amount_net = data["amount_net"]
        cost.amount_gross = cost.amount_net * (1 + (cost.vat_rate or 0))
    if "vat_rate" in data:
        cost.vat_rate = data["vat_rate"]
        cost.amount_gross = (cost.amount_net or 0) * (1 + cost.vat_rate)
    if "amount_paid" in data:
        cost.amount_paid = data["amount_paid"]
        _derive_status(cost)
    if "amount_reimbursed" in data:
        cost.amount_reimbursed = data["amount_reimbursed"]
        _derive_status(cost)
    return _refreshed(db, cost)


@router.put(
    "/cost-signals/{signal_id}/client-role", status_code=204, response_class=Response
)
def set_client_role(
    body: ClientRoleUpdate,
    db: Session = Depends(get_db),
    signal: CostSignal = Depends(require_cost_signal_access(edit=True)),
):
    """Who won the cost ruling, from the client's side; overrides any auto-detect."""
    allocation = dict(signal.allocation or {})
    if body.role == "unset":
        allocation.pop("client_role", None)
    else:
        allocation["client_role"] = body.role
    # A manual choice supersedes any prior auto-detect, so drop its markers.
    allocation.pop("auto_detected", None)
    allocation.pop("rationale", None)
    signal.allocation = allocation
    db.commit()
    if signal.case_id:
        recompute_total_cost_exposure(signal.case_id, db)


@router.post(
    "/cost-signals/{signal_id}/auto-detect-role",
    status_code=204,
    response_class=Response,
)
def auto_detect_role(
    db: Session = Depends(get_db),
    signal: CostSignal = Depends(require_cost_signal_access(edit=True)),
):
    """Ask the local model which side the ruling favours; leaves the allocation alone on 'unknown'."""
    from app.services.intelligence.cost_ruling_sider import detect_cost_ruling_role

    if signal.signal_type.value != "cost_ruling":
        raise ApiError(422, "not_a_ruling", "Signal is not a cost ruling.")
    new_alloc = detect_cost_ruling_role(signal.id, db)
    if new_alloc is not None:
        signal.allocation = new_alloc
        db.commit()
        if signal.case_id:
            recompute_total_cost_exposure(signal.case_id, db)
