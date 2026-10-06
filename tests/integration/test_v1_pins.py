"""Margin pins on key passages — /api/v1/documents/{id}/pins and /api/v1/pins/{id}."""

import pytest
from fastapi.testclient import TestClient

from app.main import app
from app.models.database import Case, CaseStatus, Document, DocumentPin

client = TestClient(app)

PASSAGE_ID = "abc123456789"


@pytest.fixture
def doc_in_case(db_session):
    case = Case(id="PIN-CASE-001", title="Pin Case", status=CaseStatus.INTAKE)
    db_session.add(case)
    db_session.flush()
    doc = Document(
        title="Pin Doc",
        case_id="PIN-CASE-001",
        content="The court orders the father to pay.\n\nFurther text follows.",
        key_passages=[
            {
                "id": PASSAGE_ID,
                "text": "The court orders the father to pay.",
                "kind": "ruling",
                "start_offset": 0,
                "end_offset": 35,
            }
        ],
    )
    db_session.add(doc)
    db_session.commit()
    return doc


def _create(doc_id, **body):
    return client.post(
        f"/api/v1/documents/{doc_id}/pins", json={"passage_id": PASSAGE_ID, **body}
    )


@pytest.mark.integration
def test_create_update_delete_pin(db_session, doc_in_case):
    created = _create(doc_in_case.id, note="check the amount")
    assert created.status_code == 201, created.text
    pin = created.json()
    assert pin["passage_id"] == PASSAGE_ID
    assert pin["note"] == "check the amount"

    reader = client.get(f"/api/v1/documents/{doc_in_case.id}/reader").json()
    assert [p["id"] for p in reader["pins"]] == [pin["id"]]
    assert reader["key_passages"][0]["pin_count"] == 1
    assert f'data-passage-id="{PASSAGE_ID}"' in reader["body_html"]

    updated = client.patch(f"/api/v1/pins/{pin['id']}", json={"note": "Updated"})
    assert updated.status_code == 200
    assert updated.json()["note"] == "Updated"
    db_session.expire_all()
    assert db_session.get(DocumentPin, pin["id"]).note == "Updated"

    assert client.delete(f"/api/v1/pins/{pin['id']}").status_code == 204
    db_session.expire_all()
    assert db_session.get(DocumentPin, pin["id"]) is None


@pytest.mark.integration
def test_pin_requires_a_known_passage(db_session, doc_in_case):
    resp = client.post(
        f"/api/v1/documents/{doc_in_case.id}/pins", json={"passage_id": "nope00000000"}
    )
    assert resp.status_code == 422
    assert resp.json()["code"] == "unknown_passage"


@pytest.mark.integration
def test_pin_routes_404_for_unknown_ids(db_session):
    assert _create(99999).status_code == 404
    assert client.patch("/api/v1/pins/99999", json={"note": "x"}).status_code == 404
    assert client.delete("/api/v1/pins/99999").status_code == 404


@pytest.mark.integration
def test_reader_shape(db_session, doc_in_case):
    resp = client.get(f"/api/v1/documents/{doc_in_case.id}/reader")
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["nav"] == {
        "prev_doc_id": None,
        "next_doc_id": None,
        "position": 0,
        "total": 0,
        "parent_id": None,
        "first_child_id": None,
        "bundle_prev_id": None,
        "bundle_next_id": None,
    }
    assert body["has_original"] is False
    assert body["thread_open"] is False
    assert body["context_strategy"] is None
    assert '<mark id="p-abc123456789"' in body["body_html"]
    assert "Further text follows." in body["body_html"]
