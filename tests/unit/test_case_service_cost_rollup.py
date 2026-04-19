"""Tests for Case.total_cost_exposure rollup."""

import pytest

from app.models.database import Case, Document
from app.models.enums import CaseStatus, IngestStatus, Jurisdiction, OriginatorType
from app.services.case_service import recompute_total_cost_exposure


@pytest.fixture
def case_with_costs(db_session):
    case = Case(
        id="COST-TEST-001",
        title="Cost Test Case",
        status=CaseStatus.INTAKE,
        jurisdiction=Jurisdiction.DE,
        total_cost_exposure=0,
    )
    db_session.add(case)

    docs = [
        Document(
            title="Invoice 1",
            case_id="COST-TEST-001",
            ingest_status=IngestStatus.COMPLETED,
            originator_type=OriginatorType.COURT,
            cost_delta={
                "amount": 450.50,
                "direction": "incoming",
                "description": "Court fee",
            },
        ),
        Document(
            title="Invoice 2",
            case_id="COST-TEST-001",
            ingest_status=IngestStatus.COMPLETED,
            originator_type=OriginatorType.OPPOSING,
            cost_delta={
                "amount": 1200.0,
                "direction": "outgoing",
                "description": "Lawyer fee",
            },
        ),
        Document(
            title="No cost doc",
            case_id="COST-TEST-001",
            ingest_status=IngestStatus.COMPLETED,
            originator_type=OriginatorType.COURT,
            cost_delta=None,
        ),
    ]
    for d in docs:
        db_session.add(d)

    db_session.commit()
    return case


@pytest.mark.unit
def test_recompute_total_cost_exposure(db_session, case_with_costs):
    result = recompute_total_cost_exposure("COST-TEST-001", db_session)

    # 450.50 + 1200.0 = 1650.50 EUR → 165050 cents
    assert result == 165050

    db_session.expire_all()
    case = db_session.get(Case, "COST-TEST-001")
    assert case.total_cost_exposure == 165050


@pytest.mark.unit
def test_recompute_ignores_triage(db_session):
    result = recompute_total_cost_exposure("_TRIAGE", db_session)
    assert result == 0


@pytest.mark.unit
def test_recompute_no_docs(db_session, sample_case):
    result = recompute_total_cost_exposure(sample_case.id, db_session)
    assert result == 0

    db_session.expire_all()
    case = db_session.get(Case, sample_case.id)
    assert case.total_cost_exposure == 0
