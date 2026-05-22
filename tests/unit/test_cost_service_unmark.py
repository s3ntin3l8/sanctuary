"""Tests for CostService.mark_as_unpaid / mark_as_unreimbursed — the
reverse-direction toggles that pair with mark_as_paid / mark_as_reimbursed."""

from datetime import datetime

import pytest

from app.models.database import Case, LegalCost
from app.models.enums import (
    CaseStatus,
    CaseType,
    CostCategory,
    CostStatus,
    Jurisdiction,
)
from app.services.cost_service import CostService


def _make_case(db):
    case = Case(
        id="C-UNMARK",
        title="Unmark test",
        status=CaseStatus.INTAKE,
        jurisdiction=Jurisdiction.DE,
        case_type=CaseType.CIVIL,
    )
    db.add(case)
    db.flush()
    return case


def _make_cost(db, case_id, *, paid=0.0, reimbursed=0.0, gross=1000.0):
    cost = LegalCost(
        case_id=case_id,
        category=CostCategory.ANWALTSKOSTEN,
        title="row",
        amount_net=gross / 1.19,
        vat_rate=0.19,
        amount_gross=gross,
        amount_paid=paid,
        amount_reimbursed=reimbursed,
        is_reimbursable=True,
        status=CostStatus.OFFEN,
    )
    db.add(cost)
    db.flush()
    return cost


@pytest.mark.unit
def test_mark_as_unpaid_round_trip(db_session):
    case = _make_case(db_session)
    cost = _make_cost(db_session, case.id)
    svc = CostService(db_session)

    paid = svc.mark_as_paid(cost.id)
    assert paid.status == CostStatus.BEZAHLT
    assert paid.amount_paid == 1000.0
    assert paid.paid_at is not None

    unpaid = svc.mark_as_unpaid(cost.id)
    assert unpaid.status == CostStatus.OFFEN
    assert unpaid.amount_paid == 0.0
    assert unpaid.paid_at is None


@pytest.mark.unit
def test_mark_as_unreimbursed_round_trip(db_session):
    case = _make_case(db_session)
    cost = _make_cost(db_session, case.id)
    svc = CostService(db_session)

    reimbursed = svc.mark_as_reimbursed(cost.id, 1000.0)
    assert reimbursed.status == CostStatus.ERSTATTET
    assert reimbursed.amount_reimbursed == 1000.0

    unrei = svc.mark_as_unreimbursed(cost.id)
    assert unrei.status == CostStatus.OFFEN
    assert unrei.amount_reimbursed == 0.0


@pytest.mark.unit
def test_unpaid_falls_back_to_erstattet_when_reimbursed(db_session):
    """If a row was paid AND reimbursed, then unpaying should leave it as
    ERSTATTET (the reimbursement is still in effect)."""
    case = _make_case(db_session)
    cost = _make_cost(db_session, case.id, paid=1000.0, reimbursed=1000.0)
    cost.status = CostStatus.ERSTATTET
    cost.paid_at = datetime.now()
    db_session.flush()
    svc = CostService(db_session)

    result = svc.mark_as_unpaid(cost.id)
    assert result.amount_paid == 0.0
    assert result.status == CostStatus.ERSTATTET


@pytest.mark.unit
def test_unreimbursed_falls_back_to_bezahlt_when_paid(db_session):
    """A row that was both paid and reimbursed, then un-reimbursed, should
    revert to BEZAHLT (the payment is still in effect)."""
    case = _make_case(db_session)
    cost = _make_cost(db_session, case.id, paid=1000.0, reimbursed=1000.0)
    cost.status = CostStatus.ERSTATTET
    cost.paid_at = datetime.now()
    db_session.flush()
    svc = CostService(db_session)

    result = svc.mark_as_unreimbursed(cost.id)
    assert result.amount_reimbursed == 0.0
    assert result.status == CostStatus.BEZAHLT


@pytest.mark.unit
def test_mark_as_unpaid_returns_none_for_missing_id(db_session):
    svc = CostService(db_session)
    assert svc.mark_as_unpaid(999_999) is None
    assert svc.mark_as_unreimbursed(999_999) is None
