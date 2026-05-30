"""Pin: CaseService.delete_empty_proceeding guards and deletes empty proceedings.

Behaviours pinned here:
- Empty proceeding with a sibling → deleted; row gone; sibling untouched.
- Proceeding with documents → ValueError; row survives.
- Proceeding with an ingest_batch (no docs) → ValueError.
- Proceeding with an action_item → ValueError.
- Proceeding with a legal_cost → ValueError.
- Last proceeding of a case (empty) → ValueError.
- Nonexistent id → ValueError("not found").
- Active proceeding deleted → return value has was_active=True.
"""

from datetime import datetime

import pytest

from app.models.database import (
    ActionItem,
    Case,
    Document,
    IngestBatch,
    LegalCost,
    Proceeding,
)
from app.models.enums import (
    ActionItemType,
    CaseStatus,
    CostCategory,
    CostStatus,
    IngestBatchSourceType,
    Jurisdiction,
    ProceedingCourtLevel,
)


def _make_case(db, case_id="PROC-DEL-1"):
    case = Case(
        id=case_id, title="Test", status=CaseStatus.INTAKE, jurisdiction=Jurisdiction.DE
    )
    db.add(case)
    db.commit()
    return case


def _make_proceeding(db, case_id, name="AG München"):
    p = Proceeding(
        case_id=case_id,
        court_name=name,
        court_level=ProceedingCourtLevel.AG,
    )
    db.add(p)
    db.commit()
    db.refresh(p)
    return p


@pytest.mark.unit
def test_delete_empty_proceeding_succeeds(db_session, sample_user):
    case = _make_case(db_session)
    p1 = _make_proceeding(db_session, case.id, "AG München")
    p2 = _make_proceeding(db_session, case.id, "LG München")

    from app.services.case_service import CaseService

    result = CaseService(db_session).delete_empty_proceeding(p2.id, sample_user.id)

    assert result["case_id"] == case.id
    assert result["was_active"] is False

    db_session.expire_all()
    assert db_session.get(Proceeding, p2.id) is None
    assert db_session.get(Proceeding, p1.id) is not None


@pytest.mark.unit
def test_delete_proceeding_with_document_raises(db_session, sample_user):
    case = _make_case(db_session)
    _make_proceeding(db_session, case.id, "AG")
    p2 = _make_proceeding(db_session, case.id, "LG")

    doc = Document(title="Brief", content="x", case_id=case.id, proceeding_id=p2.id)
    db_session.add(doc)
    db_session.commit()

    from app.services.case_service import CaseService

    with pytest.raises(ValueError, match="attached records"):
        CaseService(db_session).delete_empty_proceeding(p2.id, sample_user.id)

    db_session.expire_all()
    assert db_session.get(Proceeding, p2.id) is not None


@pytest.mark.unit
def test_delete_proceeding_with_ingest_batch_raises(db_session, sample_user):
    case = _make_case(db_session)
    _make_proceeding(db_session, case.id, "AG")
    p2 = _make_proceeding(db_session, case.id, "LG")

    batch = IngestBatch(
        source_type=IngestBatchSourceType.MANUAL,
        case_id=case.id,
        proceeding_id=p2.id,
    )
    db_session.add(batch)
    db_session.commit()

    from app.services.case_service import CaseService

    with pytest.raises(ValueError, match="attached records"):
        CaseService(db_session).delete_empty_proceeding(p2.id, sample_user.id)


@pytest.mark.unit
def test_delete_proceeding_with_action_item_raises(db_session, sample_user):
    case = _make_case(db_session)
    _make_proceeding(db_session, case.id, "AG")
    p2 = _make_proceeding(db_session, case.id, "LG")

    item = ActionItem(
        case_id=case.id,
        proceeding_id=p2.id,
        title="Frist",
        action_type=ActionItemType.DEADLINE,
        due_date=datetime(2026, 6, 1),
    )
    db_session.add(item)
    db_session.commit()

    from app.services.case_service import CaseService

    with pytest.raises(ValueError, match="attached records"):
        CaseService(db_session).delete_empty_proceeding(p2.id, sample_user.id)


@pytest.mark.unit
def test_delete_proceeding_with_legal_cost_raises(db_session, sample_user):
    case = _make_case(db_session)
    _make_proceeding(db_session, case.id, "AG")
    p2 = _make_proceeding(db_session, case.id, "LG")

    cost = LegalCost(
        case_id=case.id,
        proceeding_id=p2.id,
        category=CostCategory.GERICHTSKOSTEN,
        title="Kosten",
        amount_net=100,
        amount_gross=100,
        status=CostStatus.OFFEN,
    )
    db_session.add(cost)
    db_session.commit()

    from app.services.case_service import CaseService

    with pytest.raises(ValueError, match="attached records"):
        CaseService(db_session).delete_empty_proceeding(p2.id, sample_user.id)


@pytest.mark.unit
def test_delete_last_proceeding_raises(db_session, sample_user):
    case = _make_case(db_session)
    p = _make_proceeding(db_session, case.id, "AG")

    from app.services.case_service import CaseService

    with pytest.raises(ValueError, match="only proceeding"):
        CaseService(db_session).delete_empty_proceeding(p.id, sample_user.id)

    db_session.expire_all()
    assert db_session.get(Proceeding, p.id) is not None


@pytest.mark.unit
def test_delete_nonexistent_proceeding_raises(db_session, sample_user):
    from app.services.case_service import CaseService

    with pytest.raises(ValueError, match="not found"):
        CaseService(db_session).delete_empty_proceeding(999999, sample_user.id)


@pytest.mark.unit
def test_delete_active_proceeding_reports_was_active(db_session, sample_user):
    case = _make_case(db_session)
    _make_proceeding(db_session, case.id, "AG")
    p2 = _make_proceeding(db_session, case.id, "LG")

    from app.services.user_settings_service import set_active_proceeding

    set_active_proceeding(case.id, p2.id, db_session, sample_user.id)
    db_session.commit()

    from app.services.case_service import CaseService

    result = CaseService(db_session).delete_empty_proceeding(p2.id, sample_user.id)
    assert result["was_active"] is True
