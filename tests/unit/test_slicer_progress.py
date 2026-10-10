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


def _scan_batch(db_session, tmp_path, pages: int) -> IngestBatch:
    import pypdfium2 as pdfium

    pdf = tmp_path / "original.pdf"
    doc = pdfium.PdfDocument.new()
    for _ in range(pages):
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
    return batch


@pytest.fixture
def judged(monkeypatch):
    """Capture the boundaries prepare() hands the AI; the AI agrees with all."""
    from app.services.ingestion import slicer

    seen: list = []

    async def fake(candidates, model):
        seen.extend(candidates)
        return {
            c.page: {"is_new_document": True, "confidence": "high"} for c in candidates
        }

    monkeypatch.setattr(slicer, "_ai_cut_judgments", fake)
    return seen


@pytest.mark.unit
def test_prepare_ocrs_the_full_render_and_saves_a_small_thumbnail(
    db_session, tmp_path, monkeypatch, judged
):
    """OCR must see the full 120-DPI render; only the saved thumbnail is shrunk."""
    from app.services.ingestion import slicer

    batch = _scan_batch(db_session, tmp_path, 2)
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


@pytest.mark.unit
def test_prepare_asks_the_ai_about_every_boundary_even_without_signals(
    db_session, tmp_path, monkeypatch, judged
):
    from app.services.ingestion import slicer

    batch = _scan_batch(db_session, tmp_path, 4)
    monkeypatch.setattr(
        slicer, "_ocr_page_text", lambda img: "und deshalb geht es weiter im Text"
    )

    slicer.prepare(batch.id)

    assert [c.page for c in judged] == [2, 3, 4]
    assert all(c.signals == () for c in judged)
    db_session.refresh(batch)
    assert [c["page"] for c in batch.meta["slicing"]["proposed_cuts"]] == [2, 3, 4]


@pytest.mark.unit
def test_prepare_above_the_page_cap_judges_only_signalled_boundaries(
    db_session, tmp_path, monkeypatch, judged
):
    from app.services.ingestion import slicer

    batch = _scan_batch(db_session, tmp_path, 4)
    monkeypatch.setattr(slicer, "_AI_MAX_PAGES", 3)
    filler = "und deshalb geht es weiter im Text"
    texts = iter([filler, filler, "Anlage K 3 Kontoauszug Sparkasse", filler])
    monkeypatch.setattr(slicer, "_ocr_page_text", lambda img: next(texts))

    slicer.prepare(batch.id)

    assert [c.page for c in judged] == [3]
    assert "enclosure_marker" in judged[0].signals
