import pytest
from fastapi.testclient import TestClient

from app.main import app
from app.models.database import Document

client = TestClient(app)


@pytest.mark.integration
def test_contacts_index_deleted(db_session):
    """Index page deleted per vision §UI:382; both paths should 404."""
    assert client.get("/contacts").status_code == 404
    assert client.get("/contacts").status_code == 404


@pytest.mark.integration
def test_contacts_detail_still_works(db_session):
    """Detail route /contacts/{sender_name} remains for command palette drill-in."""
    doc = Document(
        title="Letter",
        sender="John Smith",
        case_id=None,
    )
    db_session.add(doc)
    db_session.commit()

    response = client.get("/contacts/John%20Smith")
    assert response.status_code == 200
    assert "John Smith" in response.text
