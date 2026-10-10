"""Discarding pages while slicing a scan, and the ``blank`` hint in the view."""

import pytest
from tests.unit.test_slicing_confirm import (
    _admin_id,
    _create_real_scan_batch,
    _create_scan_batch,
)


@pytest.mark.unit
def test_discard_drops_page_from_slice(db_session, tmp_path):
    from app.models.database import Document
    from app.schemas.slicing import SliceCut
    from app.services.slicing_service import confirm_slices

    batch = _create_real_scan_batch(db_session, tmp_path, 5)
    ids = confirm_slices(
        db_session, batch, [SliceCut(page=3, kind="attachment")], discard=[2]
    )
    docs = [db_session.get(Document, i) for i in ids]
    assert [d.page_count for d in docs] == [2, 2]
    assert docs[0].meta["pages"] == [1, 3]
    assert docs[1].meta["pages"] == [4, 5]


@pytest.mark.unit
def test_discarded_slice_pdf_has_fewer_pages(db_session, tmp_path):
    import pypdfium2 as pdfium

    from app.core.paths import resolve_storage_path
    from app.models.database import Document
    from app.services.slicing_service import confirm_slices

    batch = _create_real_scan_batch(db_session, tmp_path, 4)
    ids = confirm_slices(db_session, batch, [], discard=[2])
    doc = db_session.get(Document, ids[0])
    pdf = pdfium.PdfDocument(str(resolve_storage_path(doc.file_path)))
    try:
        assert len(pdf) == 3
    finally:
        pdf.close()


@pytest.mark.unit
def test_fully_discarded_slice_carries_letter_boundary(db_session, tmp_path):
    """[p1-2 | p3 separator, discarded | p4]: the separator opened a letter, so
    p4 still opens a bundle of its own."""
    from app.models.database import Document
    from app.models.enums import DocumentRole
    from app.schemas.slicing import SliceCut
    from app.services.slicing_service import confirm_slices

    batch = _create_real_scan_batch(db_session, tmp_path, 4)
    ids = confirm_slices(
        db_session,
        batch,
        [SliceCut(page=2, kind="letter"), SliceCut(page=3, kind="attachment")],
        discard=[3],
    )
    docs = [db_session.get(Document, i) for i in ids]
    assert [d.title.rsplit(" ", 1)[-1] for d in docs] == ["1", "2"]
    assert all(d.role == DocumentRole.STANDALONE for d in docs)
    assert docs[1].meta["pages"] == [4]


@pytest.mark.unit
def test_discarded_attachment_slice_keeps_the_bundle(db_session, tmp_path):
    from app.models.database import Document
    from app.models.enums import DocumentRole
    from app.schemas.slicing import SliceCut
    from app.services.slicing_service import confirm_slices

    batch = _create_real_scan_batch(db_session, tmp_path, 3)
    ids = confirm_slices(
        db_session,
        batch,
        [SliceCut(page=1, kind="attachment"), SliceCut(page=2, kind="attachment")],
        discard=[2],
    )
    docs = [db_session.get(Document, i) for i in ids]
    assert len(docs) == 2
    assert docs[0].role == DocumentRole.COVER_LETTER
    assert docs[1].role == DocumentRole.ENCLOSURE


@pytest.mark.unit
def test_discarding_every_page_returns_409(db_session, tmp_path):
    from fastapi.testclient import TestClient

    from app.main import app

    batch = _create_scan_batch(db_session, tmp_path)
    resp = TestClient(app, raise_server_exceptions=False).post(
        f"/api/v1/slicing/{batch.id}/confirm",
        json={"cuts": [], "discard": [1, 2, 3]},
    )
    assert resp.status_code == 409
    assert resp.json()["code"] == "unsliceable"


@pytest.mark.unit
def test_out_of_range_discards_are_ignored(db_session, tmp_path):
    from app.models.database import Document
    from app.services.slicing_service import confirm_slices

    batch = _create_real_scan_batch(db_session, tmp_path, 2)
    ids = confirm_slices(db_session, batch, [], discard=[0, 3, 99, -1])
    assert db_session.get(Document, ids[0]).page_count == 2


@pytest.mark.unit
def test_view_exposes_blank_flag(db_session, tmp_path):
    from app.api.v1.slicing import _view
    from app.models.database import IngestBatch
    from app.models.enums import IngestBatchSourceType, IngestBatchStatus

    batch = IngestBatch(
        owner_id=_admin_id(db_session),
        source_type=IngestBatchSourceType.SCAN,
        subject="scan.pdf",
        raw_source_path=str(tmp_path / "scan.pdf"),
        status=IngestBatchStatus.AWAITING_SLICING,
        meta={
            "slicing": {
                "status": "ready",
                "page_count": 2,
                "pages": [{"text_head": "Hello"}, {"text_head": "", "blank": True}],
            }
        },
    )
    db_session.add(batch)
    db_session.commit()
    assert [p.blank for p in _view(batch).pages] == [False, True]
