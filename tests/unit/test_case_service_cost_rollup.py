"""Tests for Case.total_cost_exposure rollup (RVG/GKG calculator-based)."""

import pytest

from app.models.database import Case, Document, LegalCost, Proceeding
from app.models.enums import (
    CaseStatus,
    CaseType,
    CostCategory,
    CostStatus,
    Jurisdiction,
    OriginatorType,
    ProceedingCourtLevel,
    ProceedingStatus,
)
from app.services.case_service import recompute_total_cost_exposure

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _make_case(db, case_id="C-001", case_type=CaseType.CIVIL, worst_case=True):
    case = Case(
        id=case_id,
        title="Test",
        status=CaseStatus.INTAKE,
        jurisdiction=Jurisdiction.DE,
        case_type=case_type,
        assume_worst_case=worst_case,
        total_cost_exposure=0,
    )
    db.add(case)
    db.flush()
    return case


def _make_proceeding(db, case_id, level=ProceedingCourtLevel.AG):
    proc = Proceeding(
        case_id=case_id,
        court_name="Amtsgericht Hamburg",
        court_level=level,
        status=ProceedingStatus.ACTIVE,
    )
    db.add(proc)
    db.flush()
    return proc


def _add_signal(db, case_id, proc_id, kind, amount=None, allocation=None):
    cd = {"kind": kind, "direction": "outgoing"}
    if amount is not None:
        cd["amount"] = amount
    if allocation is not None:
        cd["allocation"] = allocation
    doc = Document(
        title=f"Signal {kind}",
        case_id=case_id,
        proceeding_id=proc_id,
        originator_type=OriginatorType.COURT,
        cost_delta=cd,
    )
    db.add(doc)
    db.flush()
    return doc


# ---------------------------------------------------------------------------
# Guard tests
# ---------------------------------------------------------------------------


@pytest.mark.unit
def test_recompute_ignores_triage(db_session):
    assert recompute_total_cost_exposure("_TRIAGE", db_session) == 0


@pytest.mark.unit
def test_recompute_missing_case(db_session):
    assert recompute_total_cost_exposure("DOES-NOT-EXIST", db_session) == 0


@pytest.mark.unit
def test_recompute_no_proceedings(db_session, sample_case):
    result = recompute_total_cost_exposure(sample_case.id, db_session)
    assert result == 0
    db_session.expire_all()
    assert db_session.get(Case, sample_case.id).total_cost_exposure == 0


@pytest.mark.unit
def test_recompute_proceeding_without_streitwert(db_session):
    """Proceedings with no streitwert signal contribute 0 to the rollup."""
    case = _make_case(db_session)
    proc = _make_proceeding(db_session, case.id)
    # Cost-ruling signal but NO streitwert → skip
    _add_signal(db_session, case.id, proc.id, "cost_ruling", allocation={"loser": 1.0})
    db_session.commit()

    assert recompute_total_cost_exposure(case.id, db_session) == 0


# ---------------------------------------------------------------------------
# Civil case — streitwert projection
# ---------------------------------------------------------------------------


@pytest.mark.unit
def test_civil_worst_case_no_ruling_ag_10k():
    """Civil AG, streitwert €10K, worst_case=True, no ruling → full exposure."""
    # Expected (golden values from test_fees_calculator.py):
    # own lawyer gross  1850.45
    # court (×1.0)     1842.00
    # opposing (×1.0)  1850.45
    # total EUR = 5542.90 → 554290 cents
    pass


@pytest.mark.unit
def test_civil_worst_case_no_ruling(db_session):
    case = _make_case(db_session, worst_case=True)
    proc = _make_proceeding(db_session, case.id, ProceedingCourtLevel.AG)
    _add_signal(db_session, case.id, proc.id, "streitwert", amount=10_000)
    db_session.commit()

    result = recompute_total_cost_exposure(case.id, db_session)
    # own 1850.45 + court 1842.00 + opposing 1850.45 = 5542.90 → 554290
    assert result == pytest.approx(554290, abs=1)


@pytest.mark.unit
def test_civil_cost_ruling_each_own(db_session):
    """Cost ruling 'each_own' → each party bears own costs; court split 50/50."""
    case = _make_case(db_session)
    proc = _make_proceeding(db_session, case.id, ProceedingCourtLevel.AG)
    _add_signal(db_session, case.id, proc.id, "streitwert", amount=10_000)
    _add_signal(
        db_session, case.id, proc.id, "cost_ruling", allocation={"each_own": True}
    )
    db_session.commit()

    result = recompute_total_cost_exposure(case.id, db_session)
    # own 1850.45 + court×0.5 921.00 = 2771.45 → 277145
    assert result == pytest.approx(277145, abs=1)


@pytest.mark.unit
def test_civil_cost_ruling_loser_pays(db_session):
    """Cost ruling loser pays → own + full court + full opposing."""
    case = _make_case(db_session)
    proc = _make_proceeding(db_session, case.id, ProceedingCourtLevel.AG)
    _add_signal(db_session, case.id, proc.id, "streitwert", amount=10_000)
    _add_signal(db_session, case.id, proc.id, "cost_ruling", allocation={"loser": 1.0})
    db_session.commit()

    result = recompute_total_cost_exposure(case.id, db_session)
    # 1850.45 + 1842.00 + 1850.45 = 5542.90 → 554290
    assert result == pytest.approx(554290, abs=1)


# ---------------------------------------------------------------------------
# Multi-instance propagation
# ---------------------------------------------------------------------------


@pytest.mark.unit
def test_propagated_ruling_to_second_instance(db_session):
    """First-instance loser-pays ruling propagates to Berufung."""
    case = _make_case(db_session)
    proc_ag = _make_proceeding(db_session, case.id, ProceedingCourtLevel.AG)
    proc_olg = _make_proceeding(db_session, case.id, ProceedingCourtLevel.OLG)
    _add_signal(db_session, case.id, proc_ag.id, "streitwert", amount=10_000)
    _add_signal(
        db_session, case.id, proc_ag.id, "cost_ruling", allocation={"loser": 1.0}
    )
    _add_signal(db_session, case.id, proc_olg.id, "streitwert", amount=10_000)
    db_session.commit()

    result = recompute_total_cost_exposure(case.id, db_session)
    # AG: own 1850.45 + court 1842.00 + opp 1850.45 = 5542.90
    # OLG (propagated loser_pays):
    #   own gross: net=(1.6+1.2)×614+20=1739.20, gross=1739.20×1.19=2069.648→2069.65
    #   court: 614×4=2456 ×1.0=2456
    #   opp: 2069.65
    #   OLG total = 6595.30
    # Grand total = 5542.90 + 6595.30 = 12138.20 → 1213820
    assert result == pytest.approx(1213820, abs=2)


# ---------------------------------------------------------------------------
# Family law
# ---------------------------------------------------------------------------


@pytest.mark.unit
def test_family_default_each_own(db_session):
    """Family case with no ruling → each_own default (§81 FamFG)."""
    case = _make_case(db_session, case_type=CaseType.FAMILY, worst_case=False)
    proc = _make_proceeding(db_session, case.id, ProceedingCourtLevel.AG)
    _add_signal(db_session, case.id, proc.id, "streitwert", amount=3_000)
    db_session.commit()

    result = recompute_total_cost_exposure(case.id, db_session)
    # Family AG: own gross = 684.25; court = 222×2.0=444, ×0.5=222; opp=0
    # Total = 684.25 + 222.00 = 906.25 → 90625
    assert result == pytest.approx(90625, abs=1)


# ---------------------------------------------------------------------------
# LegalCost ledger integration
# ---------------------------------------------------------------------------


@pytest.mark.unit
def test_open_ledger_row_added(db_session):
    """Open LegalCost row adds to the rollup (no streitwert proceedings)."""
    case = _make_case(db_session)
    db_session.add(
        LegalCost(
            case_id=case.id,
            category=CostCategory.GERICHTSKOSTEN,
            title="GKG Vorschuss",
            amount_net=500.0,
            amount_gross=500.0,
            status=CostStatus.OFFEN,
        )
    )
    db_session.commit()

    result = recompute_total_cost_exposure(case.id, db_session)
    assert result == 50000  # 500 EUR → 50000 cents


@pytest.mark.unit
def test_paid_ledger_row_not_counted(db_session):
    """Fully-paid LegalCost row does not contribute to exposure."""
    case = _make_case(db_session)
    db_session.add(
        LegalCost(
            case_id=case.id,
            category=CostCategory.GERICHTSKOSTEN,
            title="Court fee",
            amount_net=500.0,
            amount_gross=500.0,
            amount_paid=500.0,
            status=CostStatus.BEZAHLT,
        )
    )
    db_session.commit()

    assert recompute_total_cost_exposure(case.id, db_session) == 0


@pytest.mark.unit
def test_overpaid_ledger_row_reduces_exposure(db_session):
    """Overpaid Vorschuss reduces exposure by the expected refund amount."""
    case = _make_case(db_session)
    db_session.add(
        LegalCost(
            case_id=case.id,
            category=CostCategory.VORSCHUSS,
            title="Vorschuss",
            amount_net=400.0,
            amount_gross=400.0,
            amount_paid=500.0,  # overpaid by 100
            status=CostStatus.BEZAHLT,
        )
    )
    db_session.commit()

    result = recompute_total_cost_exposure(case.id, db_session)
    assert result == -10000  # -100 EUR → -10000 cents (refund expected)


@pytest.mark.unit
def test_erstattet_row_not_counted(db_session):
    """ERSTATTET row contributes 0 — reimbursement already received."""
    case = _make_case(db_session)
    db_session.add(
        LegalCost(
            case_id=case.id,
            category=CostCategory.ANWALTSKOSTEN,
            title="Costs",
            amount_net=1000.0,
            amount_gross=1000.0,
            amount_reimbursed=1000.0,
            status=CostStatus.ERSTATTET,
        )
    )
    db_session.commit()

    assert recompute_total_cost_exposure(case.id, db_session) == 0
