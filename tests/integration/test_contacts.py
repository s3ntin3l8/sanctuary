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


@pytest.mark.integration
def test_a_contact_lists_the_addresses_it_writes_from(db_session, sample_case):
    from app.models.database import Entity
    from app.models.enums import EntityType

    db_session.add(
        Entity(
            case_id=sample_case.id,
            type=EntityType.PERSON,
            name="Hans Müller",
            email="h.mueller@kanzlei.example",
        )
    )
    db_session.add(
        Document(
            title="Schriftsatz",
            file_path="x",
            case_id=sample_case.id,
            sender="Hans Müller <hans@other.example>",
        )
    )
    db_session.commit()

    body = client.get("/api/v1/contacts", params={"name": "Hans Müller"}).json()
    assert body["emails"] == ["h.mueller@kanzlei.example", "hans@other.example"]


@pytest.mark.integration
def test_a_contact_does_not_borrow_the_address_of_a_longer_name(
    db_session, sample_case
):
    from app.models.database import Entity
    from app.models.enums import EntityType

    db_session.add_all(
        [
            Entity(
                case_id=sample_case.id,
                type=EntityType.PERSON,
                name="Hans Müller",
                email="Hans@Kanzlei.example",
            ),
            Entity(
                case_id=sample_case.id,
                type=EntityType.ORGANIZATION,
                name="Müller Bau GmbH",
                email="info@mueller-bau.example",
            ),
            Document(
                title="Schriftsatz",
                file_path="x",
                case_id=sample_case.id,
                sender="Hans Müller <hans@other.example>",
            ),
            Document(
                title="Rechnung",
                file_path="y",
                case_id=sample_case.id,
                sender="Müller Bau GmbH <info@mueller-bau.example>",
            ),
        ]
    )
    db_session.commit()

    full = client.get("/api/v1/contacts", params={"name": "Hans Müller"}).json()
    assert full["emails"] == ["hans@kanzlei.example", "hans@other.example"]
    # A partial name matches documents by substring, but borrows no address:
    # neither the entity's nor the other correspondent's.
    partial = client.get("/api/v1/contacts", params={"name": "Müller"}).json()
    assert partial["document_count"] == 2
    assert partial["emails"] == []
    by_address = client.get(
        "/api/v1/contacts", params={"name": "info@mueller-bau.example"}
    ).json()
    assert by_address["emails"] == ["info@mueller-bau.example"]
