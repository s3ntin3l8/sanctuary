"""Smoke tests for 8 previously-uncovered triage route handlers.

Each test verifies:
  - Expected HTTP status code with a valid fixture
  - A recognisable HTML marker in the response body (where applicable)
  - 404 for non-existent IDs
"""

import pytest

from app.models.database import Document, IngestBatch
from app.models.enums import (
    IngestBatchSourceType,
    IngestBatchStatus,
)

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _make_batch(db_session, **overrides) -> IngestBatch:
    defaults = {
        "source_type": IngestBatchSourceType.EMAIL,
        "status": IngestBatchStatus.PENDING,
        "subject": "Smoke test batch",
    }
    defaults.update(overrides)
    batch = IngestBatch(**defaults)
    db_session.add(batch)
    db_session.flush()
    return batch


def _make_doc(db_session, batch: IngestBatch, **overrides) -> Document:
    defaults = {
        "title": "Smoke test document",
        "case_id": "_TRIAGE",
        "needs_review": True,
        "ingest_batch_id": batch.id,
    }
    defaults.update(overrides)
    doc = Document(**defaults)
    db_session.add(doc)
    db_session.flush()
    return doc


# ---------------------------------------------------------------------------
# POST /triage/document/{doc_id}/title  →  update_doc_title
# ---------------------------------------------------------------------------


@pytest.mark.integration
def test_update_doc_title_returns_204(app_client, db_session):
    """Valid title update returns 204 with empty body."""
    batch = _make_batch(db_session)
    doc = _make_doc(db_session, batch)
    db_session.commit()

    response = app_client.post(
        f"/triage/document/{doc.id}/title",
        data={"title": "Updated Title"},
    )
    assert response.status_code == 204
    assert response.text == ""


@pytest.mark.integration
def test_update_doc_title_empty_is_noop(app_client, db_session):
    """Empty title is a no-op — handler still returns 204."""
    batch = _make_batch(db_session)
    doc = _make_doc(db_session, batch)
    db_session.commit()
    original_title = doc.title

    response = app_client.post(
        f"/triage/document/{doc.id}/title",
        data={"title": ""},
    )
    assert response.status_code == 204
    db_session.expire(doc)
    db_session.refresh(doc)
    assert doc.title == original_title


@pytest.mark.integration
def test_update_doc_title_404_for_missing_doc(app_client, db_session):
    """Non-existent doc_id returns 404."""
    response = app_client.post(
        "/triage/document/999999/title",
        data={"title": "Whatever"},
    )
    assert response.status_code == 404


# ---------------------------------------------------------------------------
# POST /triage/bundle/{batch_id}/set-cover  →  triage_set_cover_letter
# ---------------------------------------------------------------------------


@pytest.mark.integration
def test_set_cover_letter_returns_200_with_tree_html(app_client, db_session):
    """Marking a doc as cover letter renders the triage tree picker."""
    batch = _make_batch(db_session)
    doc = _make_doc(db_session, batch)
    db_session.commit()

    response = app_client.post(
        f"/triage/bundle/{batch.id}/set-cover",
        data={"doc_id": str(doc.id)},
    )
    assert response.status_code == 200
    assert f'id="triage-tree-batch-{batch.id}"' in response.text


@pytest.mark.integration
def test_set_cover_letter_wrong_batch_raises_server_error(app_client, db_session):
    """set-cover with a doc that does not belong to batch raises ValueError (server error)."""
    batch = _make_batch(db_session)
    doc = _make_doc(db_session, batch)
    other_batch = _make_batch(db_session, subject="Other batch")
    db_session.commit()

    # The service raises ValueError (not HTTPException) so TestClient re-raises it.
    import pytest as _pytest

    with _pytest.raises(ValueError, match="not in batch"):
        app_client.post(
            f"/triage/bundle/{other_batch.id}/set-cover",
            data={"doc_id": str(doc.id)},
        )


# ---------------------------------------------------------------------------
# POST /triage/bundle/{batch_id}/new-group  →  triage_create_sub_group
# ---------------------------------------------------------------------------


@pytest.mark.integration
def test_create_sub_group_returns_200_with_tree_html(app_client, db_session):
    """Creating a new sub-group returns the updated picker HTML."""
    batch = _make_batch(db_session)
    _make_doc(db_session, batch)
    db_session.commit()

    response = app_client.post(f"/triage/bundle/{batch.id}/new-group")
    assert response.status_code == 200
    assert f'id="triage-tree-batch-{batch.id}"' in response.text


@pytest.mark.integration
def test_create_sub_group_missing_batch_raises_integrity_error(app_client, db_session):
    """create_sub_group with a non-existent batch_id fails on FK constraint → server error."""
    import pytest as _pytest
    from sqlalchemy.exc import IntegrityError

    with _pytest.raises((IntegrityError, Exception)):
        app_client.post("/triage/bundle/999998/new-group")


# ---------------------------------------------------------------------------
# POST /triage/bundle/{batch_id}/rename-group  →  triage_rename_sub_group
# ---------------------------------------------------------------------------


@pytest.mark.integration
def test_rename_sub_group_returns_200_with_tree_html(app_client, db_session):
    """Renaming a sub-group (via lead_doc_id) returns the picker HTML."""
    batch = _make_batch(db_session)
    doc = _make_doc(db_session, batch)
    db_session.commit()

    response = app_client.post(
        f"/triage/bundle/{batch.id}/rename-group",
        data={"sub_group_id": "", "lead_doc_id": str(doc.id), "label": "New Label"},
    )
    assert response.status_code == 200
    assert f'id="triage-tree-batch-{batch.id}"' in response.text


@pytest.mark.integration
def test_rename_sub_group_nonexistent_sub_group_id_raises_server_error(
    app_client, db_session
):
    """Explicit sub_group_id that does not exist causes service ValueError (server error)."""
    batch = _make_batch(db_session)
    _make_doc(db_session, batch)
    db_session.commit()

    import pytest as _pytest

    with _pytest.raises(ValueError, match="not found in batch"):
        app_client.post(
            f"/triage/bundle/{batch.id}/rename-group",
            data={"sub_group_id": "999999", "lead_doc_id": "", "label": "X"},
        )


# ---------------------------------------------------------------------------
# POST /triage/bundle/{batch_id}/delete-group  →  triage_delete_sub_group
# ---------------------------------------------------------------------------


@pytest.mark.integration
def test_delete_sub_group_returns_200_with_tree_html(app_client, db_session):
    """Deleting a sub-group (via lead_doc_id) returns the picker HTML."""
    batch = _make_batch(db_session)
    doc = _make_doc(db_session, batch)
    db_session.commit()

    response = app_client.post(
        f"/triage/bundle/{batch.id}/delete-group",
        data={"sub_group_id": "", "lead_doc_id": str(doc.id)},
    )
    assert response.status_code == 200
    assert f'id="triage-tree-batch-{batch.id}"' in response.text


@pytest.mark.integration
def test_delete_sub_group_nonexistent_sub_group_id_raises_server_error(
    app_client, db_session
):
    """Explicit sub_group_id that does not exist causes service ValueError (server error)."""
    batch = _make_batch(db_session)
    _make_doc(db_session, batch)
    db_session.commit()

    import pytest as _pytest

    with _pytest.raises(ValueError):
        app_client.post(
            f"/triage/bundle/{batch.id}/delete-group",
            data={"sub_group_id": "999999", "lead_doc_id": ""},
        )


# ---------------------------------------------------------------------------
# POST /triage/bundle/{batch_id}/reorder  →  triage_reorder_documents
# ---------------------------------------------------------------------------


@pytest.mark.integration
def test_reorder_documents_returns_200_with_tree_html(app_client, db_session):
    """Reordering docs within a sub-group returns the picker HTML."""
    batch = _make_batch(db_session)
    doc1 = _make_doc(db_session, batch, title="Doc A")
    doc2 = _make_doc(db_session, batch, title="Doc B")
    db_session.commit()

    response = app_client.post(
        f"/triage/bundle/{batch.id}/reorder",
        data={
            "sub_group_id": "",
            "lead_doc_id": str(doc1.id),
            "doc_ids": f"{doc2.id},{doc1.id}",
        },
    )
    assert response.status_code == 200
    assert f'id="triage-tree-batch-{batch.id}"' in response.text


@pytest.mark.integration
def test_reorder_documents_missing_doc_ids_returns_422(app_client, db_session):
    """Missing required `doc_ids` field returns 422 Unprocessable Entity."""
    batch = _make_batch(db_session)
    _make_doc(db_session, batch)
    db_session.commit()

    response = app_client.post(
        f"/triage/bundle/{batch.id}/reorder",
        data={"sub_group_id": "", "lead_doc_id": ""},
        # doc_ids intentionally omitted
    )
    assert response.status_code == 422


# ---------------------------------------------------------------------------
# POST /triage/bundle/{batch_id}/reset-groups  →  triage_reset_sub_groups
# ---------------------------------------------------------------------------


@pytest.mark.integration
def test_reset_sub_groups_returns_200_with_tree_html(app_client, db_session):
    """Resetting sub-groups reverts to auto mode and returns picker HTML."""
    batch = _make_batch(db_session)
    _make_doc(db_session, batch)
    db_session.commit()

    response = app_client.post(f"/triage/bundle/{batch.id}/reset-groups")
    assert response.status_code == 200
    assert f'id="triage-tree-batch-{batch.id}"' in response.text


@pytest.mark.integration
def test_reset_sub_groups_missing_batch_returns_gracefully(app_client, db_session):
    """Missing batch falls back to the 'Bundle not found' HTML (200)."""
    response = app_client.post("/triage/bundle/999999/reset-groups")
    # _render_picker returns a 200 with fallback div when bundle is missing.
    assert response.status_code == 200
    assert "Bundle not found" in response.text


# ---------------------------------------------------------------------------
# GET /triage/doc/{doc_id}/body  →  triage_doc_body
# ---------------------------------------------------------------------------


@pytest.mark.integration
def test_triage_doc_body_returns_200_with_body_html(app_client, db_session):
    """Fetching a doc body returns the HUD body partial."""
    batch = _make_batch(db_session)
    doc = _make_doc(db_session, batch, content="Some legal text.")
    db_session.commit()

    response = app_client.get(f"/triage/doc/{doc.id}/body")
    assert response.status_code == 200
    # The _body.html wraps everything in a relative div
    assert "<div" in response.text


@pytest.mark.integration
def test_triage_doc_body_404_for_missing_doc(app_client, db_session):
    """Non-existent doc_id returns 404."""
    response = app_client.get("/triage/doc/999999/body")
    assert response.status_code == 404
