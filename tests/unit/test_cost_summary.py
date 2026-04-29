"""Regression tests for CostSummary NULL handling."""

from app.models.database import LegalCost
from app.models.enums import CostCategory, CostStatus
from app.services.cost_service import CostSummary


def test_total_reimbursable_handles_null_amounts():
    cost = LegalCost(
        case_id="X",
        category=CostCategory.ANWALTSKOSTEN,
        status=CostStatus.OFFEN,
        amount_gross=None,
        amount_reimbursed=None,
        is_reimbursable=True,
    )

    summary = CostSummary([cost])

    assert summary.total_reimbursable == 0


def test_total_reimbursable_handles_partial_null():
    cost = LegalCost(
        case_id="X",
        category=CostCategory.ANWALTSKOSTEN,
        status=CostStatus.OFFEN,
        amount_gross=100.0,
        amount_reimbursed=None,
        is_reimbursable=True,
    )

    summary = CostSummary([cost])

    assert summary.total_reimbursable == 100.0
