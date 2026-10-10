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
    """Capture the boundaries prepare() hands the AI; it agrees with all but page 2.

    The outline pass is stubbed out: it answers nothing, so kinds stay as judged.
    """
    from app.services.ingestion import slicer

    seen: list = []

    async def fake(candidates, model):
        seen.extend(candidates)
        return {
            c.page: {"is_new_document": c.page != 2, "confidence": "high"}
            for c in candidates
        }

    async def no_outline(prompt, n_parts, model):
        return {}

    monkeypatch.setattr(slicer, "_ai_cut_judgments", fake)
    monkeypatch.setattr(slicer, "_ai_outline", no_outline)
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
    texts = iter(
        [
            "alpha bravo charlie delta echo foxtrot golf hotel india",
            "juliet kilo lima mike november oscar papa quebec romeo",
            "sierra tango uniform victor whiskey xray yankee zulu apfel",
            "birne citrone dattel erdbeere feige guave himbeere ingwer",
        ]
    )
    monkeypatch.setattr(slicer, "_ocr_page_text", lambda img: next(texts))

    slicer.prepare(batch.id)

    assert [c.page for c in judged] == [2, 3, 4]
    assert all(c.signals == () for c in judged)
    db_session.refresh(batch)
    # the AI's verdicts, not the candidate list, decide the cuts
    assert [c["page"] for c in batch.meta["slicing"]["proposed_cuts"]] == [3, 4]


@pytest.mark.unit
def test_prepare_above_the_page_cap_judges_only_signalled_boundaries(
    db_session, tmp_path, monkeypatch, judged, caplog
):
    from app.services.ingestion import slicer

    batch = _scan_batch(db_session, tmp_path, 4)
    monkeypatch.setattr(slicer, "_AI_MAX_PAGES", 3)
    texts = iter(
        [
            "alpha bravo charlie delta echo foxtrot golf hotel india",
            "juliet kilo lima mike november oscar papa quebec romeo",
            "Anlage K 3 Kontoauszug Sparkasse",
            "sierra tango uniform victor whiskey xray yankee zulu apfel",
        ]
    )
    monkeypatch.setattr(slicer, "_ocr_page_text", lambda img: next(texts))

    slicer.prepare(batch.id)

    assert [c.page for c in judged] == [3]
    assert "enclosure_marker" in judged[0].signals
    assert "skipping 2" in caplog.text


@pytest.mark.unit
def test_prepare_takes_kinds_from_the_outline_and_records_it(
    db_session, tmp_path, monkeypatch
):
    """The per-boundary judgment says 'letter' for the forwarded part; the outline corrects it."""
    from app.services.ingestion import slicer

    batch = _scan_batch(db_session, tmp_path, 3)
    monkeypatch.setattr(
        slicer, "_ocr_page_text", lambda img: "Sehr geehrte Damen und Herren, Betreff"
    )

    async def judge(candidates, model):
        return {
            c.page: {"is_new_document": True, "kind": "letter", "notes": "new"}
            for c in candidates
        }

    prompts: list[str] = []

    async def outline(prompt, n_parts, model):
        prompts.append(prompt)
        return {
            "parts": [
                {"part": 1, "kind": "letter", "notes": "court cover"},
                {"part": 2, "kind": "attachment", "notes": "forwarded"},
                {"part": 3, "kind": "garbage"},
            ]
        }

    monkeypatch.setattr(slicer, "_ai_cut_judgments", judge)
    monkeypatch.setattr(slicer, "_ai_outline", outline)

    slicer.prepare(batch.id)

    db_session.refresh(batch)
    cuts = batch.meta["slicing"]["proposed_cuts"]
    assert [(c["page"], c["kind"]) for c in cuts] == [(2, "attachment"), (3, "letter")]
    assert "outline: forwarded" in cuts[0]["notes"]
    assert "Part 3 (pages 3-3)" in prompts[0]


@pytest.mark.unit
def test_prepare_survives_a_failing_outline(db_session, tmp_path, monkeypatch):
    from app.services.ingestion import slicer

    batch = _scan_batch(db_session, tmp_path, 3)
    monkeypatch.setattr(slicer, "_ocr_page_text", lambda img: "Sehr geehrte Damen")

    async def judge(candidates, model):
        return {c.page: {"is_new_document": True, "kind": "letter"} for c in candidates}

    async def broken(prompt, n_parts, model):
        raise RuntimeError("model down")

    monkeypatch.setattr(slicer, "_ai_cut_judgments", judge)
    monkeypatch.setattr(slicer, "_ai_outline", broken)

    slicer.prepare(batch.id)

    db_session.refresh(batch)
    assert batch.meta["slicing"]["status"] == "ready"
    assert [c["kind"] for c in batch.meta["slicing"]["proposed_cuts"]] == [
        "letter",
        "letter",
    ]
