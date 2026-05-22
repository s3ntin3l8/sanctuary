"""Smoke tests for the cost-ruling sider helper.

The LLM call is monkeypatched — these tests cover the merge logic that turns
a model verdict into a calculator-compatible allocation dict, plus the
short-circuits (missing signal, wrong signal type, no document text)."""

from __future__ import annotations

import pytest

from app.models.database import Case, CostSignal, Document
from app.models.enums import (
    CaseStatus,
    CaseType,
    CostSignalType,
    Jurisdiction,
    OriginatorType,
)
from app.services.intelligence import cost_ruling_sider
from app.services.intelligence.cost_ruling_sider import (
    CostRulingSide,
    detect_cost_ruling_role,
)


def _seed(
    db, *, allocation: dict, doc_content: str = "Die Antragsgegnerin trägt die Kosten."
):
    case = Case(
        id="C-SIDE",
        title="Sider test",
        status=CaseStatus.INTAKE,
        jurisdiction=Jurisdiction.DE,
        case_type=CaseType.CIVIL,
    )
    db.add(case)
    db.flush()
    doc = Document(
        title="Beschluss",
        case_id=case.id,
        content=doc_content,
        originator_type=OriginatorType.COURT,
    )
    db.add(doc)
    db.flush()
    signal = CostSignal(
        case_id=case.id,
        source_document_id=doc.id,
        signal_type=CostSignalType.COST_RULING,
        allocation=allocation,
    )
    db.add(signal)
    db.flush()
    return signal


@pytest.mark.unit
def test_detect_returns_none_for_missing_signal(db_session):
    assert detect_cost_ruling_role(999_999, db_session) is None


@pytest.mark.unit
def test_detect_returns_none_for_non_ruling_signal(db_session):
    case = Case(
        id="C-NO",
        title="x",
        status=CaseStatus.INTAKE,
        jurisdiction=Jurisdiction.DE,
    )
    db_session.add(case)
    db_session.flush()
    doc = Document(
        title="x",
        case_id=case.id,
        content="x",
        originator_type=OriginatorType.COURT,
    )
    db_session.add(doc)
    db_session.flush()
    sig = CostSignal(
        case_id=case.id,
        source_document_id=doc.id,
        signal_type=CostSignalType.STREITWERT,
        amount=10000.0,
    )
    db_session.add(sig)
    db_session.flush()
    assert detect_cost_ruling_role(sig.id, db_session) is None


@pytest.mark.unit
def test_detect_returns_none_when_doc_has_no_text(db_session):
    signal = _seed(db_session, allocation={"loser": 1.0}, doc_content="")
    assert detect_cost_ruling_role(signal.id, db_session) is None


@pytest.mark.unit
def test_detect_winner_produces_calculator_compatible_allocation(
    db_session, monkeypatch
):
    signal = _seed(db_session, allocation={"loser": 1.0})

    monkeypatch.setattr(
        cost_ruling_sider,
        "call_json_ai",
        lambda **_: CostRulingSide(
            client_role="winner",
            rationale="Die Antragsgegnerin trägt die Kosten.",
        ),
    )

    result = detect_cost_ruling_role(signal.id, db_session)
    assert result is not None
    assert result["loser"] == 1.0
    assert result["client_role"] == "winner"
    assert result["auto_detected"] is True
    assert "Antragsgegnerin" in result["rationale"]


@pytest.mark.unit
def test_detect_each_own_produces_each_own_shape(db_session, monkeypatch):
    signal = _seed(db_session, allocation={"loser": 1.0})

    monkeypatch.setattr(
        cost_ruling_sider,
        "call_json_ai",
        lambda **_: CostRulingSide(client_role="each_own", rationale="§81 FamFG"),
    )

    result = detect_cost_ruling_role(signal.id, db_session)
    assert result is not None
    assert result.get("each_own") is True
    assert result["client_role"] == "each_own"
    assert result["auto_detected"] is True


@pytest.mark.unit
def test_detect_unknown_returns_none(db_session, monkeypatch):
    signal = _seed(db_session, allocation={"loser": 1.0})

    monkeypatch.setattr(
        cost_ruling_sider,
        "call_json_ai",
        lambda **_: CostRulingSide(client_role="unknown", rationale="ambiguous"),
    )

    assert detect_cost_ruling_role(signal.id, db_session) is None


@pytest.mark.unit
def test_detect_swallows_llm_exception(db_session, monkeypatch):
    signal = _seed(db_session, allocation={"loser": 1.0})

    def boom(**_):
        raise RuntimeError("ollama unreachable")

    monkeypatch.setattr(cost_ruling_sider, "call_json_ai", boom)

    assert detect_cost_ruling_role(signal.id, db_session) is None
