"""Turn a scanned multi-page PDF into separate documents at user-chosen cuts."""

from __future__ import annotations

import hashlib
from pathlib import Path

from sqlalchemy.orm import Session

from app.core.paths import resolve_storage_path, to_storage_path
from app.models.database import Document, IngestBatch
from app.models.enums import IngestBatchStatus


class SlicingFailed(RuntimeError):
    """A slice could not be written; everything written so far was removed."""


def confirm_slices(
    db: Session, batch: IngestBatch, cut_positions: list[int]
) -> list[int]:
    """Split the batch's source PDF after each page in ``cut_positions``.

    Creates one ``_TRIAGE`` document per slice (the first one wired as the
    cover letter of the rest), flips the batch to PROCESSING and returns the
    new document ids. The caller dispatches processing and must hold the
    batch row lock. Raises ``ValueError`` when the batch cannot be sliced and
    :class:`SlicingFailed` when writing fails (slice files are cleaned up).
    """
    from app.services.ingestion.cover_letter_wiring import wire_cover_letter

    if batch.status != IngestBatchStatus.AWAITING_SLICING:
        raise ValueError("Batch is not awaiting slicing")
    slicing_meta = (batch.meta or {}).get("slicing", {})
    page_count = slicing_meta.get("page_count", 0)
    if not page_count:
        raise ValueError("Batch slicing metadata missing")
    cut_positions = sorted({c for c in cut_positions if 1 <= c < page_count})
    if not batch.raw_source_path:
        raise ValueError("Source PDF no longer available")
    pdf_path = resolve_storage_path(batch.raw_source_path)
    if not pdf_path.exists():
        raise ValueError("Source PDF no longer available")

    boundaries = [0] + cut_positions + [page_count]
    slices = [
        (boundaries[i] + 1, boundaries[i + 1]) for i in range(len(boundaries) - 1)
    ]

    import pypdfium2 as pdfium

    docs_to_process: list[Document] = []
    first_doc_id: int | None = None
    written_slice_paths: list[Path] = []

    try:
        src_pdf = pdfium.PdfDocument(str(pdf_path))

        for slice_idx, (start_page, end_page) in enumerate(slices):
            slice_pdf = pdfium.PdfDocument.new()
            page_indices = list(range(start_page - 1, end_page))
            slice_pdf.import_pages(src_pdf, page_indices)

            slice_filename = pdf_path.parent / f"slice_{slice_idx + 1}.pdf"
            slice_pdf.save(str(slice_filename))
            slice_pdf.close()
            written_slice_paths.append(slice_filename)

            slice_bytes = slice_filename.read_bytes()
            content_hash = hashlib.sha256(slice_bytes).hexdigest()

            slice_page_count = end_page - start_page + 1

            doc = Document(
                title=f"{pdf_path.stem} – Part {slice_idx + 1}",
                owner_id=batch.owner_id,  # sliced docs inherit the batch's owner
                file_path=to_storage_path(slice_filename),
                original_filename=slice_filename.name,
                content_hash=content_hash,
                case_id="_TRIAGE",
                ingest_batch_id=batch.id,
                meta={"slice_range": [start_page, end_page]},
                page_count=slice_page_count,
            )
            from app.services.pipeline_status import initialize as _pipeline_init

            db.add(doc)
            db.flush()
            _pipeline_init(doc, batched=True, db=db)
            docs_to_process.append(doc)

            if slice_idx == 0:
                first_doc_id = doc.id

        src_pdf.close()

        # Wire cover letter + enclosures
        if first_doc_id and len(docs_to_process) > 1:
            child_ids = [d.id for d in docs_to_process[1:]]
            wire_cover_letter(db, first_doc_id, child_ids, court_relay=True)

        batch.status = IngestBatchStatus.PROCESSING
        db.commit()

    except Exception as exc:
        db.rollback()
        # The DB rows for these slices are gone (rolled back), but the slice
        # PDFs already written to disk are not — remove them so a retry
        # doesn't inherit stale/orphaned files from this failed attempt.
        for path in written_slice_paths:
            if path.exists():
                try:
                    path.unlink()
                except OSError:
                    pass
        raise SlicingFailed(f"Slicing failed: {exc}") from exc

    return [d.id for d in docs_to_process]
