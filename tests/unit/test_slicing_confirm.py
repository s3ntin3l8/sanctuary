"""Unit tests for the slicing confirm endpoint."""

import pytest


def _make_minimal_pdf_bytes() -> bytes:
    return b"""%PDF-1.4
1 0 obj<</Type /Catalog /Pages 2 0 R>>endobj
2 0 obj<</Type /Pages /Kids [3 0 R] /Count 1>>endobj
3 0 obj<</Type /Page /MediaBox [0 0 612 792]>>endobj
xref
0 4
0000000000 65535 f
trailer<</Size 4 /Root 1 0 R>>
startxref
0
%%EOF"""


def _create_scan_batch(db_session, tmp_path, page_count=3):
    from app.models.database import IngestBatch
    from app.models.enums import IngestBatchSourceType, IngestBatchStatus

    pdf = tmp_path / "original.pdf"
    pdf.write_bytes(_make_minimal_pdf_bytes())

    batch = IngestBatch(
        owner_id=_admin_id(db_session),
        source_type=IngestBatchSourceType.SCAN,
        subject="test_scan.pdf",
        raw_source_path=str(pdf),
        status=IngestBatchStatus.AWAITING_SLICING,
        meta={"slicing": {"status": "ready", "page_count": page_count}},
    )
    db_session.add(batch)
    db_session.commit()
    db_session.refresh(batch)
    return batch


def _admin_id(db) -> int:
    from app.models.database import User

    return db.query(User).filter_by(email="admin@localhost").one().id


def _create_real_scan_batch(db_session, tmp_path, page_count=3):
    """Like _create_scan_batch, but with a real N-page PDF pdfium can
    actually split — needed for tests that exercise the slicing loop itself
    rather than just the pre-loop guards."""
    import pypdfium2 as pdfium

    from app.models.database import IngestBatch
    from app.models.enums import IngestBatchSourceType, IngestBatchStatus

    pdf = tmp_path / "original.pdf"
    doc = pdfium.PdfDocument.new()
    for _ in range(page_count):
        doc.new_page(200, 200)
    doc.save(str(pdf))
    doc.close()

    batch = IngestBatch(
        owner_id=_admin_id(db_session),
        source_type=IngestBatchSourceType.SCAN,
        subject="test_scan.pdf",
        raw_source_path=str(pdf),
        status=IngestBatchStatus.AWAITING_SLICING,
        meta={"slicing": {"status": "ready", "page_count": page_count}},
    )
    db_session.add(batch)
    db_session.commit()
    db_session.refresh(batch)
    return batch


@pytest.mark.unit
def test_slicing_confirm_idempotency_guard(db_session, tmp_path):
    """A batch not in AWAITING_SLICING should be rejected / redirected."""
    from app.models.database import IngestBatch
    from app.models.enums import IngestBatchSourceType, IngestBatchStatus

    pdf = tmp_path / "original.pdf"
    pdf.write_bytes(b"%PDF-1.4")
    batch = IngestBatch(
        owner_id=_admin_id(db_session),
        source_type=IngestBatchSourceType.SCAN,
        subject="already_done.pdf",
        raw_source_path=str(pdf),
        status=IngestBatchStatus.PROCESSING,  # already past AWAITING_SLICING
        meta={"slicing": {"status": "ready", "page_count": 2}},
    )
    db_session.add(batch)
    db_session.commit()

    from fastapi.testclient import TestClient

    from app.main import app

    client = TestClient(app, raise_server_exceptions=False)
    resp = client.post(
        f"/api/v1/slicing/{batch.id}/confirm",
        json={"cuts": []},
    )
    # Refused (409), no new Documents
    assert resp.status_code == 409
    assert resp.json()["code"] == "not_awaiting"
    from app.models.database import Document

    docs = db_session.query(Document).filter(Document.ingest_batch_id == batch.id).all()
    assert docs == []


@pytest.mark.unit
def test_slicing_confirm_locks_batch_row_with_select_for_update(
    db_session, test_engine, tmp_path
):
    """F3.6: the idempotency guard must be a real row lock, not just a
    refresh -- a plain db.refresh() lets two concurrent confirms both
    observe AWAITING_SLICING and both proceed to slice (the bug this
    replaces; the old code's own comment claimed FOR UPDATE semantics that
    the code never actually implemented). A TestClient can't reproduce the
    race directly (both requests run sequentially on one event loop), so
    this asserts the mechanism itself: the batch SELECT the endpoint issues
    is a real `SELECT ... FOR UPDATE`."""
    from sqlalchemy import event

    batch = _create_scan_batch(db_session, tmp_path, page_count=1)

    statements: list[str] = []

    def _capture(conn, cursor, statement, parameters, context, executemany):
        statements.append(statement)

    event.listen(test_engine, "before_cursor_execute", _capture)
    try:
        from fastapi.testclient import TestClient

        from app.main import app

        client = TestClient(app, raise_server_exceptions=False)
        client.post(
            f"/api/v1/slicing/{batch.id}/confirm",
            json={"cuts": []},
        )
    finally:
        event.remove(test_engine, "before_cursor_execute", _capture)

    batch_selects = [
        s
        for s in statements
        if "ingest_batches" in s.lower() and s.lower().lstrip().startswith("select")
    ]
    assert batch_selects, "expected at least one SELECT against ingest_batches"
    assert any("for update" in s.lower() for s in batch_selects), (
        "slicing_confirm's batch lookup must use SELECT ... FOR UPDATE — "
        f"got: {batch_selects}"
    )


@pytest.mark.unit
def test_slicing_confirm_rereads_status_under_the_lock(db_session, tmp_path):
    """The owner guard loads the batch before the FOR UPDATE get; SQLAlchemy
    keeps the already-loaded attributes, so without a refresh the status
    check would see a stale AWAITING_SLICING. Simulate the loser of the
    race: flip the status behind the session's back right after the guard."""
    from fastapi import Depends
    from sqlalchemy import text
    from sqlalchemy.orm import Session

    from app.api.v1 import slicing as slicing_api
    from app.dependencies import get_current_user, get_db
    from app.models.database import Document, IngestBatch, User
    from app.models.enums import IngestBatchStatus

    batch = _create_scan_batch(db_session, tmp_path, page_count=1)
    original = slicing_api.owned_batch

    def _guard_then_flip(
        batch_id: int,
        db: Session = Depends(get_db),
        user: User = Depends(get_current_user),
    ) -> IngestBatch:
        found = original(batch_id, db, user)
        db_session.execute(
            text("UPDATE ingest_batches SET status = :s WHERE id = :id"),
            {"s": IngestBatchStatus.PROCESSING.name, "id": found.id},
        )
        db_session.commit()
        return found

    from fastapi.testclient import TestClient

    from app.main import app

    app.dependency_overrides[original] = _guard_then_flip
    try:
        client = TestClient(app, raise_server_exceptions=False)
        resp = client.post(f"/api/v1/slicing/{batch.id}/confirm", json={"cuts": []})
    finally:
        app.dependency_overrides.pop(original, None)
    assert resp.status_code == 409, resp.text
    assert resp.json()["code"] == "not_awaiting"
    assert (
        db_session.query(Document).filter(Document.ingest_batch_id == batch.id).all()
        == []
    )


@pytest.mark.unit
@pytest.mark.parametrize("bad_cuts", [5, None, [None], "oops"])
def test_slicing_confirm_invalid_cuts_returns_422_not_500(
    db_session, tmp_path, bad_cuts
):
    """Non-list or non-integer cuts are rejected by validation, never a 500."""
    from fastapi.testclient import TestClient

    from app.main import app

    batch = _create_scan_batch(db_session, tmp_path, page_count=3)

    client = TestClient(app, raise_server_exceptions=False)
    resp = client.post(f"/api/v1/slicing/{batch.id}/confirm", json={"cuts": bad_cuts})
    assert resp.status_code == 422
    assert resp.json()["code"] == "validation_error"


@pytest.mark.unit
def test_slicing_confirm_happy_path_creates_expected_slices(db_session, tmp_path):
    from fastapi.testclient import TestClient

    from app.main import app
    from app.models.database import Document

    batch = _create_real_scan_batch(db_session, tmp_path, page_count=3)

    client = TestClient(app, raise_server_exceptions=False)
    resp = client.post(
        f"/api/v1/slicing/{batch.id}/confirm",
        json={"cuts": [{"page": 2, "kind": "attachment"}]},
    )
    assert resp.status_code == 200
    assert len(resp.json()["document_ids"]) == 2

    db_session.expire_all()
    docs = (
        db_session.query(Document)
        .filter(Document.ingest_batch_id == batch.id)
        .order_by(Document.id)
        .all()
    )
    assert len(docs) == 2
    assert (tmp_path / "slice_1.pdf").exists()
    assert (tmp_path / "slice_2.pdf").exists()


@pytest.mark.unit
def test_slicing_confirm_failure_removes_written_slice_files(
    db_session, tmp_path, monkeypatch
):
    """Regression: a mid-slicing failure rolled back the DB rows for the
    slices already written, but left the slice_N.pdf files themselves on
    disk — orphaned with no DB row pointing at them, and a retry of the same
    batch would then collide with (or silently resurrect) stale files."""
    from fastapi.testclient import TestClient

    from app.main import app

    batch = _create_real_scan_batch(db_session, tmp_path, page_count=3)

    def _boom(*args, **kwargs):
        raise RuntimeError("bundle wiring exploded")

    monkeypatch.setattr("app.services.slicing_service._wire_bundles", _boom)

    client = TestClient(app, raise_server_exceptions=False)
    resp = client.post(
        f"/api/v1/slicing/{batch.id}/confirm",
        json={"cuts": [{"page": 2, "kind": "attachment"}]},
    )
    assert resp.status_code == 500

    assert not (tmp_path / "slice_1.pdf").exists()
    assert not (tmp_path / "slice_2.pdf").exists()


@pytest.mark.unit
def test_slicing_confirm_titles_slices_from_batch_subject(db_session, tmp_path):
    """Uploaded scans are stored as original.pdf; slices keep the real name."""
    import pypdfium2 as pdfium

    from app.models.database import Document, IngestBatch
    from app.models.enums import IngestBatchSourceType, IngestBatchStatus
    from app.schemas.slicing import SliceCut
    from app.services.slicing_service import confirm_slices

    pdf = pdfium.PdfDocument.new()
    for _ in range(2):
        pdf.new_page(200, 300)
    path = tmp_path / "original.pdf"
    pdf.save(str(path))
    batch = IngestBatch(
        owner_id=_admin_id(db_session),
        source_type=IngestBatchSourceType.SCAN,
        subject="Gerichtspost.pdf",
        raw_source_path=str(path),
        status=IngestBatchStatus.AWAITING_SLICING,
        meta={"slicing": {"status": "ready", "page_count": 2}},
    )
    db_session.add(batch)
    db_session.commit()

    ids = confirm_slices(db_session, batch, [SliceCut(page=1, kind="attachment")])
    titles = sorted(db_session.get(Document, i).title for i in ids)
    assert titles == ["Gerichtspost – Part 1", "Gerichtspost – Part 2"]


def _stack(db_session, tmp_path, pages):
    return _create_real_scan_batch(db_session, tmp_path, page_count=pages)


@pytest.mark.unit
def test_slicing_confirm_builds_one_bundle_per_letter(db_session, tmp_path):
    """[letter p1-2 | attachment p3 | letter p4 | attachment p5-6] → two bundles."""
    from app.models.database import BatchSubGroup, Document, DocumentRelationship
    from app.models.enums import (
        DocumentRole,
        RelationshipConfidence,
        RelationshipType,
    )
    from app.schemas.slicing import SliceCut
    from app.services.slicing_service import confirm_slices

    batch = _stack(db_session, tmp_path, 6)
    ids = confirm_slices(
        db_session,
        batch,
        [
            SliceCut(page=2, kind="attachment"),
            SliceCut(page=3, kind="letter"),
            SliceCut(page=4, kind="attachment"),
        ],
    )
    docs = [db_session.get(Document, i) for i in ids]
    first, att1, second, att2 = docs
    assert [d.role for d in docs] == [
        DocumentRole.COVER_LETTER,
        DocumentRole.ENCLOSURE,
        DocumentRole.COVER_LETTER,
        DocumentRole.ENCLOSURE,
    ]
    assert att1.parent_id == first.id and att2.parent_id == second.id
    assert first.parent_id is None and second.parent_id is None
    assert not any(d.court_relay for d in docs)

    edges = db_session.query(DocumentRelationship).all()
    assert {(e.from_document_id, e.to_document_id) for e in edges} == {
        (first.id, att1.id),
        (second.id, att2.id),
    }
    assert all(
        e.relationship_type == RelationshipType.ENCLOSES
        and e.confidence == RelationshipConfidence.USER_CREATED
        for e in edges
    )

    groups = (
        db_session.query(BatchSubGroup)
        .filter_by(batch_id=batch.id)
        .order_by(BatchSubGroup.sort_order)
        .all()
    )
    assert len(groups) == 2
    assert [d.sub_group_id for d in docs] == [
        groups[0].id,
        groups[0].id,
        groups[1].id,
        groups[1].id,
    ]
    assert [d.sub_group_sort_order for d in docs] == [0, 1, 0, 1]


@pytest.mark.unit
def test_slicing_confirm_separate_letters_are_standalone(db_session, tmp_path):
    from app.models.database import Document
    from app.models.enums import DocumentRole
    from app.schemas.slicing import SliceCut
    from app.services.slicing_service import confirm_slices

    batch = _stack(db_session, tmp_path, 2)
    ids = confirm_slices(db_session, batch, [SliceCut(page=1, kind="letter")])
    docs = [db_session.get(Document, i) for i in ids]
    assert [d.role for d in docs] == [DocumentRole.STANDALONE] * 2
    assert all(d.parent_id is None for d in docs)


@pytest.mark.unit
def test_slicing_confirm_single_document_has_no_sub_group(db_session, tmp_path):
    from app.models.database import BatchSubGroup, Document
    from app.models.enums import DocumentRole
    from app.services.slicing_service import confirm_slices

    batch = _stack(db_session, tmp_path, 2)
    ids = confirm_slices(db_session, batch, [])
    assert db_session.get(Document, ids[0]).role == DocumentRole.STANDALONE
    assert db_session.query(BatchSubGroup).filter_by(batch_id=batch.id).count() == 0


@pytest.mark.unit
def test_slicing_confirm_rejects_unknown_kind_with_422(db_session, tmp_path):
    from fastapi.testclient import TestClient

    from app.main import app

    batch = _create_scan_batch(db_session, tmp_path)
    resp = TestClient(app, raise_server_exceptions=False).post(
        f"/api/v1/slicing/{batch.id}/confirm",
        json={"cuts": [{"page": 1, "kind": "memo"}]},
    )
    assert resp.status_code == 422
