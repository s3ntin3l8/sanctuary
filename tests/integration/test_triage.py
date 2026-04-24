import pytest
from fastapi.testclient import TestClient

from app.main import app
from app.models.database import (
    Case,
    CaseStatus,
    Document,
)

client = TestClient(app)


@pytest.mark.integration
def test_triage_page_renders(db_session):
    """Test triage page renders without errors."""
    response = client.get("/triage")
    assert response.status_code == 200


@pytest.mark.integration
def test_triage_with_pending_docs(db_session):
    """Test triage page shows pending documents."""
    doc = Document(
        title="Test Document for Review",
        case_id=None,
        needs_review=True,
    )
    db_session.add(doc)
    db_session.commit()

    response = client.get("/triage")
    assert response.status_code == 200


@pytest.mark.integration
def test_triage_with_case_mapped(db_session):
    """Test triage page shows case-mapped documents."""
    case = Case(id="TRIAGE-TEST-001", title="Test Case", status=CaseStatus.INTAKE)
    db_session.add(case)
    db_session.commit()

    doc = Document(
        title="Document with Case",
        case_id="TRIAGE-TEST-001",
        needs_review=True,
    )
    db_session.add(doc)
    db_session.commit()

    response = client.get("/triage")
    assert response.status_code == 200


@pytest.mark.integration
def test_triage_doc_pane_renders_embedded_hud(db_session):
    """GET /document/:id?context=triage returns the embedded HUD."""
    doc = Document(
        title="Embedded HUD Test Doc",
        content="Some content",
        case_id=None,
        needs_review=True,
    )
    db_session.add(doc)
    db_session.commit()

    response = client.get(f"/document/{doc.id}?context=triage")
    assert response.status_code == 200
    assert 'data-hud-context="embedded"' in response.text
    assert "Embedded HUD Test Doc" in response.text


@pytest.mark.integration
def test_triage_reaction_routes_removed(db_session):
    """The old triage reaction endpoints must no longer exist."""
    doc = Document(title="Reaction Test Doc", needs_review=True)
    db_session.add(doc)
    db_session.commit()

    r = client.post(
        f"/triage/document/{doc.id}/reaction",
        data={"reaction": "true"},
    )
    assert r.status_code == 404 or r.status_code == 405

    r = client.request(
        "DELETE",
        f"/triage/document/{doc.id}/reaction/true",
    )
    assert r.status_code == 404 or r.status_code == 405


@pytest.mark.integration
def test_unified_reaction_route_returns_hud_fragment(db_session):
    """POST /document/:id/reaction returns the _reactions.html fragment."""
    doc = Document(title="Reaction Fragment Test", needs_review=True)
    db_session.add(doc)
    db_session.commit()

    response = client.post(
        f"/document/{doc.id}/reaction",
        data={"reaction": "true"},
    )
    assert response.status_code == 200
    assert "hud-reaction-bar" in response.text
    assert "data-triage-reaction-bar" in response.text
    assert 'data-reaction-key="true"' in response.text


@pytest.mark.integration
def test_unified_reaction_emits_note_saved_trigger(db_session):
    """POST /document/:id/reaction with notes emits triage:note-saved trigger."""
    doc = Document(title="Note Saved Test", needs_review=True)
    db_session.add(doc)
    db_session.commit()

    response = client.post(
        f"/document/{doc.id}/reaction",
        data={"reaction": "true", "notes": "Important evidence"},
    )
    assert response.status_code == 200
    hx_trigger = response.headers.get("hx-trigger", "")
    assert "triage:note-saved" in hx_trigger


@pytest.mark.integration
def test_triage_case_selector(db_session):
    """Test triage page includes case selector."""
    response = client.get("/triage")
    assert response.status_code == 200
