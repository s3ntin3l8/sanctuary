"""Integration: DELETE /proceedings/{id} endpoint."""

import pytest

from app.models.database import Case, Document, Proceeding
from app.models.enums import (
    CaseStatus,
    Jurisdiction,
    ProceedingCourtLevel,
)


def _make_case(db, case_id="PDEL-INT-1"):
    case = Case(
        id=case_id, title="Test", status=CaseStatus.INTAKE, jurisdiction=Jurisdiction.DE
    )
    db.add(case)
    db.commit()
    return case


def _make_proceeding(db, case_id, name="AG München"):
    p = Proceeding(
        case_id=case_id, court_name=name, court_level=ProceedingCourtLevel.AG
    )
    db.add(p)
    db.commit()
    db.refresh(p)
    return p


@pytest.mark.integration
def test_delete_empty_proceeding_returns_hx_refresh(app_client, db_session):
    case = _make_case(db_session)
    p1 = _make_proceeding(db_session, case.id, "AG")
    p2 = _make_proceeding(db_session, case.id, "LG")

    response = app_client.delete(f"/proceedings/{p2.id}")

    assert response.status_code == 200
    assert response.headers.get("hx-refresh") == "true"

    p2_id, p1_id = p2.id, p1.id
    db_session.expunge_all()
    assert db_session.query(Proceeding).filter_by(id=p2_id).first() is None
    assert db_session.query(Proceeding).filter_by(id=p1_id).first() is not None


@pytest.mark.integration
def test_delete_proceeding_with_document_returns_400(app_client, db_session):
    case = _make_case(db_session, "PDEL-INT-2")
    _make_proceeding(db_session, case.id, "AG")
    p2 = _make_proceeding(db_session, case.id, "LG")

    doc = Document(title="Brief", content="x", case_id=case.id, proceeding_id=p2.id)
    db_session.add(doc)
    db_session.commit()

    response = app_client.delete(f"/proceedings/{p2.id}")
    assert response.status_code == 400

    db_session.expire_all()
    assert db_session.get(Proceeding, p2.id) is not None


@pytest.mark.integration
def test_delete_only_proceeding_returns_400(app_client, db_session):
    case = _make_case(db_session, "PDEL-INT-3")
    p = _make_proceeding(db_session, case.id, "AG")

    response = app_client.delete(f"/proceedings/{p.id}")
    assert response.status_code == 400

    db_session.expire_all()
    assert db_session.get(Proceeding, p.id) is not None


@pytest.mark.integration
def test_delete_nonexistent_proceeding_returns_404(app_client, db_session):
    response = app_client.delete("/proceedings/999999")
    assert response.status_code == 404


@pytest.mark.integration
def test_delete_active_proceeding_clears_user_setting(app_client, db_session):
    case = _make_case(db_session, "PDEL-INT-4")
    _make_proceeding(db_session, case.id, "AG")
    p2 = _make_proceeding(db_session, case.id, "LG")

    from app.services.user_settings_service import (
        get_active_proceeding,
        set_active_proceeding,
    )

    set_active_proceeding(case.id, p2.id, db_session)
    db_session.commit()

    assert get_active_proceeding(case.id, db_session) == p2.id

    response = app_client.delete(f"/proceedings/{p2.id}")
    assert response.status_code == 200

    db_session.expire_all()
    assert get_active_proceeding(case.id, db_session) is None
