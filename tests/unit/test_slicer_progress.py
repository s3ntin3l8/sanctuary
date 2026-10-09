"""Slicing prep progress is merged into meta, never replacing the recovery clock."""

import pytest
from PIL import Image

from app.models.database import IngestBatch, User
from app.models.enums import IngestBatchSourceType, IngestBatchStatus
from app.services.ingestion.slicer import _write_progress


@pytest.mark.unit
def test_progress_keeps_dispatch_metadata(db_session):
    owner = db_session.query(User).filter_by(email="admin@localhost").one()
    batch = IngestBatch(
        owner_id=owner.id,
        source_type=IngestBatchSourceType.SCAN,
        status=IngestBatchStatus.AWAITING_SLICING,
        meta={"slicing": {"status": "preparing", "dispatched_at": "2026-10-09T10:00"}},
    )
    db_session.add(batch)
    db_session.commit()

    _write_progress(db_session, batch, 5, 69, "ocr")

    db_session.refresh(batch)
    slicing = batch.meta["slicing"]
    assert slicing["dispatched_at"] == "2026-10-09T10:00"
    assert slicing["status"] == "preparing"
    assert slicing["progress"] == {"done": 5, "total": 69, "phase": "ocr"}


@pytest.mark.unit
def test_prepare_ocrs_the_full_render_and_saves_a_small_thumbnail(
    db_session, tmp_path, monkeypatch
):
    """OCR must see the full 120-DPI render; only the saved thumbnail is shrunk."""
    import pypdfium2 as pdfium

    from app.services.ingestion import slicer

    pdf = tmp_path / "original.pdf"
    doc = pdfium.PdfDocument.new()
    for _ in range(2):
        doc.new_page(600, 900)
    doc.save(str(pdf))
    doc.close()
    owner = db_session.query(User).filter_by(email="admin@localhost").one()
    batch = IngestBatch(
        owner_id=owner.id,
        source_type=IngestBatchSourceType.SCAN,
        raw_source_path=str(pdf),
        status=IngestBatchStatus.AWAITING_SLICING,
        meta={"slicing": {"status": "preparing"}},
    )
    db_session.add(batch)
    db_session.commit()

    seen: list[tuple[int, int]] = []
    monkeypatch.setattr(
        slicer, "_ocr_page_text", lambda img: seen.append(img.size) or "text"
    )

    slicer.prepare(batch.id)

    assert len(seen) == 2
    assert all(max(size) > slicer._THUMBNAIL_LONG_EDGE for size in seen)
    db_session.refresh(batch)
    slicing = batch.meta["slicing"]
    assert slicing["status"] == "ready"
    thumb = Image.open(slicing["pages"][0]["thumbnail_path"])
    assert max(thumb.size) == slicer._THUMBNAIL_LONG_EDGE
