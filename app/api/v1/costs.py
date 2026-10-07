"""Cost rows and cost-signal roles as the case dashboard and the costs page
mutate them. Each call returns the fresh row; the client
refetches the case financials for the derived totals."""

from __future__ import annotations

from fastapi import APIRouter, Depends, Response
from sqlalchemy.orm import Session

from app.api.access_guards import (
    require_case_access,
    require_cost_access,
    require_cost_signal_access,
)
from app.api.v1.case_detail import _cost_row, summary_of
from app.api.v1.errors import ApiError
from app.core.timezone import now_utc
from app.dependencies import get_current_user, get_db
from app.models.database import Case, CostSignal, LegalCost, Proceeding, User
from app.schemas.case_detail import (
    ClientRoleUpdate,
    CostAlert,
    CostCaseGroup,
    CostCreate,
    CostFieldUpdate,
    CostReimburse,
    CostRow,
    CostsOverview,
)
from app.services import access_service
from app.services.case_service import recompute_total_cost_exposure
from app.services.cost_service import CostService, _derive_status, costs_due

router = APIRouter(tags=["costs"])


@router.get("/costs", response_model=CostsOverview)
def costs_overview(
    db: Session = Depends(get_db),
    user: User = Depends(get_current_user),
):
    """The ledger across every case the caller may see."""
    visible = access_service.visible_case_ids(db, user)
    editable = access_service.editable_case_ids(db, user)
    q = db.query(LegalCost)
    if visible is not None:
        q = q.filter(LegalCost.case_id.in_(visible))
    costs = q.order_by(
        LegalCost.issued_at.desc().nullslast(), LegalCost.id.desc()
    ).all()
    by_case: dict[str, list[LegalCost]] = {}
    for c in costs:
        by_case.setdefault(c.case_id, []).append(c)
    cases = (
        db.query(Case).filter(Case.id.in_(by_case)).order_by(Case.title.asc()).all()
        if by_case
        else []
    )
    titles = {c.id: c.title for c in cases}
    alerts = [
        (
            d.overdue,
            CostAlert(
                cost=_cost_row(d.cost),
                case_title=titles.get(d.cost.case_id, d.cost.case_id),
                open_amount=d.open_amount,
            ),
        )
        for d in costs_due(costs, now_utc())
    ]
    overdue = [a for is_overdue, a in alerts if is_overdue]
    due_soon = [a for is_overdue, a in alerts if not is_overdue]
    return CostsOverview(
        summary=summary_of(costs, sum(c.total_cost_exposure or 0 for c in cases)),
        overdue=overdue,
        due_soon=due_soon,
        cases=[
            CostCaseGroup(
                id=case.id,
                title=case.title,
                status=case.status,
                can_edit=editable is None or case.id in editable,
                summary=summary_of(by_case[case.id], case.total_cost_exposure or 0),
                costs=[_cost_row(c) for c in by_case[case.id]],
            )
            for case in cases
        ],
    )


@router.post("/cases/{case_id}/costs", response_model=CostRow, status_code=201)
def create_cost(
    body: CostCreate,
    db: Session = Depends(get_db),
    case: Case = Depends(require_case_access(edit=True)),
):
    if body.proceeding_id is not None and (
        not db.query(Proceeding.id)
        .filter(Proceeding.id == body.proceeding_id, Proceeding.case_id == case.id)
        .first()
    ):
        raise ApiError(
            422, "bad_proceeding", "Proceeding does not belong to this case."
        )
    cost = CostService(db).create_cost(
        case_id=case.id,
        category=body.category,
        title=body.title,
        amount_net=body.amount_net,
        amount_gross=round(body.amount_net * (1 + body.vat_rate), 2),
        status=body.status,
        vat_rate=body.vat_rate,
        rvg_position=body.rvg_position,
        streitwert=body.streitwert,
        gebuehren_faktor=body.gebuehren_faktor,
        notes=body.notes,
        is_reimbursable=body.is_reimbursable,
        issued_at=body.issued_at,
        due_at=body.due_at,
        proceeding_id=body.proceeding_id,
    )
    db.commit()
    db.refresh(cost)
    recompute_total_cost_exposure(case.id, db)
    return _cost_row(cost)


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
    body: CostReimburse | None = None,
    db: Session = Depends(get_db),
    cost: LegalCost = Depends(require_cost_access(edit=True)),
):
    """Book a reimbursement (§91 ZPO); the full gross unless an amount is given."""
    amount = body.amount if body and body.amount is not None else cost.amount_gross
    CostService(db).mark_as_reimbursed(cost.id, amount)
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
    data = body.model_dump(exclude_unset=True)
    if not data:
        raise ApiError(422, "empty_update", "Nothing to update.")
    if (title := data.get("title")) is not None:
        cost.title = title.strip() or cost.title
    if (category := data.get("category")) is not None:
        cost.category = category
    if (amount_net := data.get("amount_net")) is not None:
        cost.amount_net = amount_net
    if (vat_rate := data.get("vat_rate")) is not None:
        cost.vat_rate = vat_rate
    if amount_net is not None or vat_rate is not None:
        cost.amount_gross = round(
            (cost.amount_net or 0) * (1 + (cost.vat_rate or 0)), 2
        )
    if (amount_paid := data.get("amount_paid")) is not None:
        cost.amount_paid = amount_paid
    if (amount_reimbursed := data.get("amount_reimbursed")) is not None:
        cost.amount_reimbursed = amount_reimbursed
    if amount_paid is not None or amount_reimbursed is not None:
        _derive_status(cost)
    if (status := data.get("status")) is not None:
        # An explicit status wins over the one derived from the amounts.
        cost.status = status
    if (is_reimbursable := data.get("is_reimbursable")) is not None:
        cost.is_reimbursable = is_reimbursable
    # Nullable fields: an explicit null clears the value.
    for field in ("streitwert", "gebuehren_faktor", "issued_at", "due_at", "notes"):
        if field in data:
            setattr(cost, field, data[field])
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
