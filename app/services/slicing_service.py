"""Turn a scanned multi-page PDF into separate documents at user-chosen cuts."""

from __future__ import annotations

import hashlib
from pathlib import Path

from sqlalchemy.orm import Session

from app.core.paths import resolve_storage_path, to_storage_path
from app.models.database import BatchSubGroup, Document, IngestBatch
from app.models.enums import (
    DocumentRole,
    IngestBatchStatus,
    RelationshipConfidence,
    RelationshipType,
)
from app.repositories.document_relationship import insert_edge_if_absent
from app.schemas.slicing import SliceCut


class SlicingFailed(RuntimeError):
    """A slice could not be written; everything written so far was removed."""


def confirm_slices(
    db: Session,
    batch: IngestBatch,
    cuts: list[SliceCut],
    discard: list[int] | None = None,
) -> list[int]:
    """Split the batch's source PDF after each page in ``cuts``.

    Creates one ``_TRIAGE`` document per slice and groups them into bundles: a
    ``letter`` cut opens a new bundle, an ``attachment`` cut adds the next part
    to the current one (the first part always opens the first bundle). Pages in
    ``discard`` are left out of every part; a part left with no pages is skipped
    and a letter cut that opened it carries to the next kept part. A bundle
    of several parts gets its first part as cover letter and the rest as
    enclosures. Each bundle becomes a ``BatchSubGroup``, so the structure is the
    user's and batch analysis leaves roles alone. Flips the batch to PROCESSING
    and returns the new document ids. The caller dispatches processing and must
    hold the batch row lock. Raises ``ValueError`` when the batch cannot be
    sliced and :class:`SlicingFailed` when writing fails (slice files are
    cleaned up).
    """
    if batch.status != IngestBatchStatus.AWAITING_SLICING:
        raise ValueError("Batch is not awaiting slicing")
    slicing_meta = (batch.meta or {}).get("slicing", {})
    page_count = slicing_meta.get("page_count", 0)
    if not page_count:
        raise ValueError("Batch slicing metadata missing")
    kinds: dict[int, str] = {}
    for cut in cuts:
        if 1 <= cut.page < page_count:
            kinds.setdefault(cut.page, cut.kind)
    cut_positions = sorted(kinds)
    discard_set = {p for p in discard or [] if 1 <= p <= page_count}
    if len(discard_set) >= page_count:
        raise ValueError("Every page is discarded; nothing to slice")
    if not batch.raw_source_path:
        raise ValueError("Source PDF no longer available")
    pdf_path = resolve_storage_path(batch.raw_source_path)
    if not pdf_path.exists():
        raise ValueError("Source PDF no longer available")

    # Uploads are stored as original.pdf; the batch subject keeps the real name.
    base_name = Path(batch.subject).stem if batch.subject else pdf_path.stem

    boundaries = [0] + cut_positions + [page_count]
    slices = [
        (boundaries[i] + 1, boundaries[i + 1]) for i in range(len(boundaries) - 1)
    ]

    import pypdfium2 as pdfium

    docs_to_process: list[Document] = []
    written_slice_paths: list[Path] = []

    try:
        src_pdf = pdfium.PdfDocument(str(pdf_path))

        # kept: (0-based page indices, opens a new letter). A skipped slice's
        # letter opener carries to the next kept slice.
        kept: list[tuple[list[int], bool]] = []
        carry_letter = False
        for slice_idx, (start_page, end_page) in enumerate(slices):
            opens_letter = slice_idx == 0 or kinds[boundaries[slice_idx]] == "letter"
            indices = [
                p - 1 for p in range(start_page, end_page + 1) if p not in discard_set
            ]
            if not indices:
                carry_letter = carry_letter or opens_letter
                continue
            kept.append((indices, opens_letter or carry_letter))
            carry_letter = False

        bundles: list[list[Document]] = []
        for slice_idx, (page_indices, opens_letter) in enumerate(kept):
            slice_pdf = pdfium.PdfDocument.new()
            slice_pdf.import_pages(src_pdf, page_indices)

            slice_filename = pdf_path.parent / f"slice_{slice_idx + 1}.pdf"
            slice_pdf.save(str(slice_filename))
            slice_pdf.close()
            written_slice_paths.append(slice_filename)

            slice_bytes = slice_filename.read_bytes()
            content_hash = hashlib.sha256(slice_bytes).hexdigest()

            doc = Document(
                title=f"{base_name} – Part {slice_idx + 1}",
                owner_id=batch.owner_id,  # sliced docs inherit the batch's owner
                file_path=to_storage_path(slice_filename),
                original_filename=slice_filename.name,
                content_hash=content_hash,
                case_id="_TRIAGE",
                ingest_batch_id=batch.id,
                meta={
                    "slice_range": [page_indices[0] + 1, page_indices[-1] + 1],
                    "pages": [p + 1 for p in page_indices],
                },
                page_count=len(page_indices),
            )
            from app.services.pipeline_status import initialize as _pipeline_init

            db.add(doc)
            db.flush()
            _pipeline_init(doc, batched=True, db=db)
            docs_to_process.append(doc)
            if slice_idx == 0 or opens_letter:
                bundles.append([])
            bundles[-1].append(doc)

        src_pdf.close()

        _wire_bundles(db, batch.id, bundles)

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


def _wire_bundles(db: Session, batch_id: int, bundles: list[list[Document]]) -> None:
    """Roles, parent links, ENCLOSES edges and one sub-group per bundle."""
    grouped = sum(len(members) for members in bundles) > 1
    for order, members in enumerate(bundles):
        lead, attachments = members[0], members[1:]
        lead.role = (
            DocumentRole.COVER_LETTER if attachments else DocumentRole.STANDALONE
        )
        for att in attachments:
            att.role = DocumentRole.ENCLOSURE
            att.parent_id = lead.id
            insert_edge_if_absent(
                db,
                from_document_id=lead.id,
                to_document_id=att.id,
                relationship_type=RelationshipType.ENCLOSES,
                confidence=RelationshipConfidence.USER_CREATED,
                notes="scan slicing: attachment",
            )
        if grouped:
            group = BatchSubGroup(batch_id=batch_id, label=None, sort_order=order)
            db.add(group)
            db.flush()
            for pos, doc in enumerate(members):
                doc.sub_group_id = group.id
                doc.sub_group_sort_order = pos
