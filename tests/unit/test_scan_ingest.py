"""Unit tests for scan ingest (batch orchestrator + Celery task)."""

import hashlib
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest


def _make_minimal_pdf(tmp_path: Path, name: str = "test.pdf") -> Path:
    """Write a tiny but structurally valid PDF."""
    content = b"""%PDF-1.4
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
    p = tmp_path / name
    p.write_bytes(content)
    return p


@pytest.mark.unit
def test_ingest_scanned_file_single_page_dispatches_process_task(db_session, tmp_path):
    """Single-page PDF: creates Document, status=PROCESSING, dispatches process_document_task."""
    from app.services.ingestion.batch_orchestrator import ingest_scanned_file

    pdf_path = _make_minimal_pdf(tmp_path)
    source_hash = hashlib.sha256(pdf_path.read_bytes()).hexdigest()

    dispatched = []
    with (
        patch("app.services.ingestion.batch_orchestrator.pdfium") as mock_pdfium,
        patch(
            "app.services.ingestion.batch_orchestrator._dispatch",
            side_effect=lambda name, doc_id: dispatched.append(name),
        ),
    ):
        mock_pdf_doc = MagicMock()
        mock_pdf_doc.__len__ = MagicMock(return_value=1)
        mock_pdfium.PdfDocument.return_value = mock_pdf_doc

        batch = ingest_scanned_file(db_session, pdf_path, "test-batch-id", source_hash)

    assert batch is not None
    assert batch.source_hash == source_hash
    from app.models.enums import IngestBatchSourceType, IngestBatchStatus

    assert batch.source_type == IngestBatchSourceType.SCAN
    assert batch.status == IngestBatchStatus.PROCESSING
    assert len(dispatched) == 1
    assert "process_document_task" in dispatched[0]


@pytest.mark.unit
def test_ingest_scanned_file_multi_page_sets_awaiting_slicing(db_session, tmp_path):
    """Multi-page PDF: status=AWAITING_SLICING, no Documents created, prepare_slicing dispatched."""
    from app.services.ingestion.batch_orchestrator import ingest_scanned_file

    pdf_path = _make_minimal_pdf(tmp_path)
    source_hash = hashlib.sha256(pdf_path.read_bytes()).hexdigest()

    dispatched = []
    with (
        patch("app.services.ingestion.batch_orchestrator.pdfium") as mock_pdfium,
        patch(
            "app.services.ingestion.batch_orchestrator._dispatch",
            side_effect=lambda name, doc_id: dispatched.append(name),
        ),
    ):
        mock_pdf_doc = MagicMock()
        mock_pdf_doc.__len__ = MagicMock(return_value=5)
        mock_pdfium.PdfDocument.return_value = mock_pdf_doc

        batch = ingest_scanned_file(
            db_session, pdf_path, "test-batch-id-2", source_hash
        )

    assert batch is not None
    from app.models.enums import IngestBatchStatus

    assert batch.status == IngestBatchStatus.AWAITING_SLICING
    assert batch.meta["slicing"]["status"] == "preparing"
    assert batch.meta["slicing"]["page_count"] == 5
    assert len(dispatched) == 1
    assert "prepare_slicing" in dispatched[0]

    # No Documents should be created
    from app.models.database import Document

    docs = db_session.query(Document).filter(Document.ingest_batch_id == batch.id).all()
    assert docs == []


@pytest.mark.unit
def test_ingest_scanned_file_dedup_returns_none(db_session, tmp_path):
    """Re-dropping the same file (same hash) returns None — duplicate silently skipped."""
    from app.services.ingestion.batch_orchestrator import ingest_scanned_file

    pdf_path = _make_minimal_pdf(tmp_path)
    source_hash = hashlib.sha256(pdf_path.read_bytes()).hexdigest()

    with (
        patch("app.services.ingestion.batch_orchestrator.pdfium") as mock_pdfium,
        patch("app.services.ingestion.batch_orchestrator._dispatch"),
    ):
        mock_pdf_doc = MagicMock()
        mock_pdf_doc.__len__ = MagicMock(return_value=1)
        mock_pdfium.PdfDocument.return_value = mock_pdf_doc

        first = ingest_scanned_file(db_session, pdf_path, "batch-1", source_hash)
        second = ingest_scanned_file(db_session, pdf_path, "batch-2", source_hash)

    assert first is not None
    assert second is None


@pytest.mark.unit
def test_scan_folder_tick_task_runs(db_session):
    """The Celery task invokes scan_and_ingest and returns status=ok."""
    from app.tasks.scan_ingest import scan_folder_tick_task

    with (
        patch("app.config.SessionLocal") as mock_sl,
        patch(
            "app.services.ingestion.scan_folder.scan_and_ingest", return_value=2
        ) as mock_scan,
    ):
        mock_sl.return_value = db_session
        result = scan_folder_tick_task.run()

    assert result["status"] == "ok"
    assert result["ingested"] == 2
    mock_scan.assert_called_once()
