"""Page viewer endpoint and the triage slicing-queue item."""

import pytest
from tests.unit.test_slicing_confirm import _admin_id


def _two_page_pdf(path):
    import pypdfium2 as pdfium

    pdf = pdfium.PdfDocument.new()
    pdf.new_page(200, 300)
    pdf.new_page(200, 300)
    pdf.save(str(path))
    pdf.close()


def _batch(db_session, pdf, meta=None):
    from app.models.database import IngestBatch
    from app.models.enums import IngestBatchSourceType, IngestBatchStatus

    batch = IngestBatch(
        owner_id=_admin_id(db_session),
        source_type=IngestBatchSourceType.SCAN,
        subject="scan.pdf",
        raw_source_path=str(pdf),
        status=IngestBatchStatus.AWAITING_SLICING,
        meta=meta or {"slicing": {"status": "ready", "page_count": 2}},
    )
    db_session.add(batch)
    db_session.commit()
    db_session.refresh(batch)
    return batch


@pytest.mark.unit
def test_page_endpoint_renders_png_and_404s_out_of_range(db_session, tmp_path):
    from fastapi.testclient import TestClient

    from app.main import app

    pdf = tmp_path / "scan.pdf"
    _two_page_pdf(pdf)
    batch = _batch(db_session, pdf)
    client = TestClient(app, raise_server_exceptions=False)

    ok = client.get(f"/api/v1/slicing/{batch.id}/page/2")
    assert ok.status_code == 200
    assert ok.headers["content-type"] == "image/png"
    assert ok.content[:8] == b"\x89PNG\r\n\x1a\n"
    assert client.get(f"/api/v1/slicing/{batch.id}/page/3").status_code == 404
    assert client.get(f"/api/v1/slicing/{batch.id}/page/0").status_code == 404


@pytest.mark.unit
def test_slicing_queue_item_carries_progress_and_cut_count(db_session, tmp_path):
    from app.api.v1.triage import _slicing_queue_item

    pdf = tmp_path / "scan.pdf"
    _two_page_pdf(pdf)
    preparing = _batch(
        db_session,
        pdf,
        {
            "slicing": {
                "status": "preparing",
                "page_count": 69,
                "progress": {"done": 23, "total": 69, "phase": "ocr"},
            }
        },
    )
    item = _slicing_queue_item(preparing)
    assert (item.progress_done, item.progress_total, item.progress_phase) == (
        23,
        69,
        "ocr",
    )
    assert item.proposed_cut_count is None

    ready = _batch(
        db_session,
        pdf,
        {
            "slicing": {
                "status": "ready",
                "page_count": 2,
                "proposed_cuts": [{"page": 2}],
            }
        },
    )
    assert _slicing_queue_item(ready).proposed_cut_count == 1


@pytest.mark.unit
def test_page_endpoint_404s_on_unreadable_pdf(db_session, tmp_path):
    from fastapi.testclient import TestClient

    from app.main import app

    pdf = tmp_path / "broken.pdf"
    pdf.write_bytes(b"not a pdf")
    batch = _batch(db_session, pdf)
    client = TestClient(app, raise_server_exceptions=False)
    assert client.get(f"/api/v1/slicing/{batch.id}/page/1").status_code == 404
