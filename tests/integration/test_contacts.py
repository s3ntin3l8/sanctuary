import pytest
from fastapi.testclient import TestClient

from app.main import app
from app.models.database import Document

client = TestClient(app)


@pytest.mark.integration
def test_contacts_index_deleted(db_session):
    """No contact directory (vision §UI:382): the API needs a name, the old path is gone."""
    assert client.get("/api/v1/contacts").status_code == 422
    assert client.get("/contacts/John").status_code == 404


@pytest.mark.integration
def test_contact_detail_on_v1(db_session):
    """GET /api/v1/contacts?name= backs the contact page the palette opens."""
    doc = Document(
        title="Letter",
        sender="John Smith",
        case_id=None,
    )
    db_session.add(doc)
    db_session.commit()

    response = client.get("/api/v1/contacts?name=John%20Smith")
    assert response.status_code == 200, response.text
    body = response.json()
    assert body["name"] == "John Smith"
    assert body["document_count"] == 1
    assert body["documents"][0]["title"] == "Letter"
    assert client.get("/contacts?name=John").status_code == 200  # SPA shell
