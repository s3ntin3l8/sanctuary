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
        f"/ingest/slice/{batch.id}/confirm",
        data={"cuts": "[]"},
        follow_redirects=False,
    )
    # Should redirect to /triage (303), not create new Documents
    assert resp.status_code in (303, 200)
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
            f"/ingest/slice/{batch.id}/confirm",
            data={"cuts": "[]"},
            follow_redirects=False,
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
