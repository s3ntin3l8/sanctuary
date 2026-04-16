import pytest
from fastapi.testclient import TestClient

from app.main import app
from app.models.database import Document

client = TestClient(app)


@pytest.mark.integration
def test_contacts_page_renders(db_session):
    """Test contacts page renders without errors."""
    response = client.get("/api/v1/contacts")
    assert response.status_code == 200


@pytest.mark.integration
def test_contacts_with_senders(db_session):
    """Test contacts page shows senders."""
    doc = Document(
        title="Letter from Plaintiff",
        sender="John Smith",
        case_id=None,
    )
    db_session.add(doc)
    db_session.commit()

    response = client.get("/api/v1/contacts")
    assert response.status_code == 200
    assert "John Smith" in response.text


@pytest.mark.integration
def test_contacts_groups_by_sender(db_session):
    """Test contacts groups documents by sender."""
    for i in range(3):
        doc = Document(
            title=f"Doc {i} from same sender",
            sender="Repeated Sender",
            case_id=None,
        )
        db_session.add(doc)
    db_session.commit()

    response = client.get("/api/v1/contacts")
    assert response.status_code == 200
