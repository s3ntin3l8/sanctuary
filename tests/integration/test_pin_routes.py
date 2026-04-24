import pytest
from fastapi.testclient import TestClient

from app.main import app
from app.models.database import Case, CaseStatus, Document

client = TestClient(app)


@pytest.fixture
def doc_in_case(db_session):
    case = Case(
        id="PIN-CASE-001", title="Pin Integration Case", status=CaseStatus.INTAKE
    )
    db_session.add(case)
    db_session.flush()
    doc = Document(title="Pin Integration Doc", case_id="PIN-CASE-001")
    db_session.add(doc)
    db_session.commit()
    return doc


@pytest.mark.integration
def test_create_pin_returns_card_fragment(db_session, doc_in_case):
    response = client.post(
        f"/document/{doc_in_case.id}/pin",
        data={"passage_id": "abc123456789"},
    )
    assert response.status_code == 200
    assert "hud-pin-card" in response.text or "pin-" in response.text
    assert "push_pin" in response.text


@pytest.mark.integration
def test_create_pin_with_note(db_session, doc_in_case):
    response = client.post(
        f"/document/{doc_in_case.id}/pin",
        data={"passage_id": "abc123456789", "note": "Critical finding"},
    )
    assert response.status_code == 200
    assert "Critical finding" in response.text


@pytest.mark.integration
def test_create_pin_nonexistent_doc(db_session):
    response = client.post("/document/99999/pin", data={"passage_id": "abc123456789"})
    assert response.status_code == 404


@pytest.mark.integration
def test_update_pin(db_session, doc_in_case):
    create_resp = client.post(
        f"/document/{doc_in_case.id}/pin",
        data={"passage_id": "abc123456789", "note": "Original"},
    )
    assert create_resp.status_code == 200

    from app.repositories.document_pin import DocumentPinRepository

    repo = DocumentPinRepository(db_session)
    pins = repo.get_by_document(doc_in_case.id)
    assert len(pins) == 1
    pin_id = pins[0].id

    patch_resp = client.patch(f"/pin/{pin_id}", data={"note": "Updated"})
    assert patch_resp.status_code == 204

    db_session.expire_all()
    updated = repo.get(pin_id)
    assert updated.note == "Updated"


@pytest.mark.integration
def test_update_pin_nonexistent(db_session):
    response = client.patch("/pin/99999", data={"note": "nope"})
    assert response.status_code == 404


@pytest.mark.integration
def test_delete_pin(db_session, doc_in_case):
    create_resp = client.post(
        f"/document/{doc_in_case.id}/pin",
        data={"passage_id": "abc123456789"},
    )
    assert create_resp.status_code == 200

    from app.repositories.document_pin import DocumentPinRepository

    repo = DocumentPinRepository(db_session)
    pins = repo.get_by_document(doc_in_case.id)
    assert len(pins) == 1
    pin_id = pins[0].id

    delete_resp = client.delete(f"/pin/{pin_id}")
    assert delete_resp.status_code == 200
    assert delete_resp.text == ""

    db_session.expire_all()
    assert repo.get(pin_id) is None


@pytest.mark.integration
def test_delete_pin_nonexistent(db_session):
    response = client.delete("/pin/99999")
    assert response.status_code == 404


@pytest.mark.integration
def test_document_detail_htmx_returns_embedded_hud(db_session, doc_in_case):
    """GET /document/:id with HX-Request header returns embedded read-mode HUD."""
    response = client.get(
        f"/document/{doc_in_case.id}",
        headers={"HX-Request": "true"},
    )
    assert response.status_code == 200
    assert 'data-hud-context="embedded"' in response.text


@pytest.mark.integration
def test_document_detail_no_htmx_redirects_to_fullscreen(db_session, doc_in_case):
    """GET /document/:id without HX-Request redirects to full-screen HUD."""
    response = client.get(
        f"/document/{doc_in_case.id}",
        follow_redirects=False,
    )
    assert response.status_code == 302
    assert (
        f"/cases/PIN-CASE-001/document/{doc_in_case.id}" in response.headers["location"]
    )


@pytest.mark.integration
def test_document_detail_unassigned_redirects_to_triage(db_session):
    """GET /document/:id for an unassigned doc redirects to /triage."""
    doc = Document(title="Unassigned Doc", case_id=None)
    db_session.add(doc)
    db_session.commit()

    response = client.get(f"/document/{doc.id}", follow_redirects=False)
    assert response.status_code == 302
    assert response.headers["location"] == "/triage"


@pytest.mark.integration
def test_document_detail_context_triage_still_works(db_session, doc_in_case):
    """GET /document/:id?context=triage still returns embedded review-mode HUD."""
    response = client.get(f"/document/{doc_in_case.id}?context=triage")
    assert response.status_code == 200
    assert 'data-hud-context="embedded"' in response.text
