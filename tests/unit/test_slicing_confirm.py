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
def test_wire_cover_letter_sets_roles_and_parent(db_session):
    """wire_cover_letter correctly sets COVER_LETTER + ENCLOSURE roles with parent_id."""
    from app.models.database import Document
    from app.models.enums import DocumentRole
    from app.services.ingestion.cover_letter_wiring import wire_cover_letter

    doc1 = Document(title="p1", file_path="/tmp/s1.pdf", case_id="_TRIAGE")
    doc2 = Document(title="p2", file_path="/tmp/s2.pdf", case_id="_TRIAGE")
    doc3 = Document(title="p3", file_path="/tmp/s3.pdf", case_id="_TRIAGE")
    db_session.add_all([doc1, doc2, doc3])
    db_session.flush()

    wire_cover_letter(db_session, doc1.id, [doc2.id, doc3.id], court_relay=True)
    db_session.commit()

    db_session.refresh(doc1)
    db_session.refresh(doc2)
    db_session.refresh(doc3)

    assert doc1.role == DocumentRole.COVER_LETTER
    assert doc1.court_relay is True
    assert doc1.parent_id is None

    assert doc2.role == DocumentRole.ENCLOSURE
    assert doc2.parent_id == doc1.id

    assert doc3.role == DocumentRole.ENCLOSURE
    assert doc3.parent_id == doc1.id


@pytest.mark.unit
def test_cover_letter_wiring_idempotent(db_session):
    """Calling wire_cover_letter twice is safe."""
    from app.models.database import Document
    from app.models.enums import DocumentRole
    from app.services.ingestion.cover_letter_wiring import wire_cover_letter

    cover = Document(title="cover", file_path="/tmp/c.pdf", case_id="_TRIAGE")
    child = Document(title="child", file_path="/tmp/k.pdf", case_id="_TRIAGE")
    db_session.add_all([cover, child])
    db_session.flush()

    wire_cover_letter(db_session, cover.id, [child.id], court_relay=True)
    wire_cover_letter(db_session, cover.id, [child.id], court_relay=True)
    db_session.commit()

    db_session.refresh(cover)
    db_session.refresh(child)
    assert cover.role == DocumentRole.COVER_LETTER
    assert child.parent_id == cover.id


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
        json={"cuts": [2]},
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
        raise RuntimeError("cover-letter wiring exploded")

    monkeypatch.setattr(
        "app.services.ingestion.cover_letter_wiring.wire_cover_letter", _boom
    )

    client = TestClient(app, raise_server_exceptions=False)
    resp = client.post(
        f"/api/v1/slicing/{batch.id}/confirm",
        json={"cuts": [2]},
    )
    assert resp.status_code == 500

    assert not (tmp_path / "slice_1.pdf").exists()
    assert not (tmp_path / "slice_2.pdf").exists()
