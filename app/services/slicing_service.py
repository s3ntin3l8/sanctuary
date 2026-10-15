"""Turn a scanned multi-page PDF into separate documents at user-chosen cuts."""

from __future__ import annotations

import hashlib
import shutil
from pathlib import Path

from sqlalchemy import func, or_
from sqlalchemy.orm import Session

from app.core.paths import resolve_storage_path, to_storage_path
from app.models.database import (
    BatchSubGroup,
    ClaimEvidence,
    Document,
    DocumentPin,
    DocumentRelationship,
    IngestBatch,
    UserReaction,
)
from app.models.enums import (
    DocumentRole,
    IngestBatchStatus,
    RelationshipConfidence,
    RelationshipType,
)
from app.repositories.document_relationship import insert_edge_if_absent
from app.schemas.slicing import SliceCut
from app.services import family_context
from app.services.ingestion import page_text


class SlicingFailed(RuntimeError):
    """A slice could not be written; everything written so far was removed."""


class SourceHasUserData(ValueError):
    """The document carries work of the user (or an agent) that a split would lose."""

    def __init__(self, reasons: list[str]):
        super().__init__(
            "The document has your own work attached: " + ", ".join(reasons)
        )
        self.reasons = reasons


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
    _, kept = _plan_parts(page_count, cuts, discard)
    if not batch.raw_source_path:
        raise ValueError("Source PDF no longer available")
    pdf_path = resolve_storage_path(batch.raw_source_path)
    if not pdf_path.exists():
        raise ValueError("Source PDF no longer available")

    # Uploads are stored as original.pdf; the batch subject keeps the real name.
    base_name = Path(batch.subject).stem if batch.subject else pdf_path.stem

    import pypdfium2 as pdfium

    docs_to_process: list[Document] = []
    written_slice_paths: list[Path] = []

    try:
        src_pdf = pdfium.PdfDocument(str(pdf_path))

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


def _plan_parts(
    page_count: int, cuts: list[SliceCut], discard: list[int] | None
) -> tuple[set[int], list[tuple[list[int], bool]]]:
    """Turn cuts and discarded pages into the parts to write.

    Returns the discarded pages and, per kept part, its 0-based page indices and
    whether it opens a new letter. A part left with no pages is skipped and its
    letter opener carries to the next kept part.
    """
    kinds: dict[int, str] = {}
    for cut in cuts:
        if 1 <= cut.page < page_count:
            kinds.setdefault(cut.page, cut.kind)
    cut_positions = sorted(kinds)
    discard_set = {p for p in discard or [] if 1 <= p <= page_count}
    if len(discard_set) >= page_count:
        raise ValueError("Every page is discarded; nothing to slice")

    boundaries = [0] + cut_positions + [page_count]
    kept: list[tuple[list[int], bool]] = []
    carry_letter = False
    for slice_idx in range(len(boundaries) - 1):
        opens_letter = slice_idx == 0 or kinds[boundaries[slice_idx]] == "letter"
        indices = [
            p - 1
            for p in range(boundaries[slice_idx] + 1, boundaries[slice_idx + 1] + 1)
            if p not in discard_set
        ]
        if not indices:
            carry_letter = carry_letter or opens_letter
            continue
        kept.append((indices, opens_letter or carry_letter))
        carry_letter = False
    return discard_set, kept


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


def user_data_reasons(db: Session, doc: Document) -> list[str]:
    """What on ``doc`` was made by a person or an agent, not by the pipeline."""
    reasons: list[str] = []
    if db.query(Document.id).filter(Document.parent_id == doc.id).first():
        reasons.append("enclosures")
    if db.query(DocumentPin.id).filter(DocumentPin.document_id == doc.id).first():
        reasons.append("pins")
    if db.query(UserReaction.id).filter(UserReaction.document_id == doc.id).first():
        reasons.append("reactions")
    human = (
        RelationshipConfidence.USER_CONFIRMED,
        RelationshipConfidence.USER_CREATED,
    )
    if (
        db.query(ClaimEvidence.id)
        .filter(
            ClaimEvidence.document_id == doc.id,
            or_(
                ClaimEvidence.confidence.in_(human),
                ClaimEvidence.agent_token_id.isnot(None),
            ),
        )
        .first()
    ):
        reasons.append("claim evidence")
    if (
        db.query(DocumentRelationship.id)
        .filter(
            or_(
                DocumentRelationship.from_document_id == doc.id,
                DocumentRelationship.to_document_id == doc.id,
            ),
            or_(
                DocumentRelationship.confidence.in_(human),
                DocumentRelationship.agent_token_id.isnot(None),
            ),
        )
        .first()
    ):
        reasons.append("relationships")
    return reasons


def confirm_document_slices(
    db: Session,
    doc: Document,
    cuts: list[SliceCut],
    discard: list[int] | None = None,
) -> list[int]:
    """Replace an extracted PDF document by parts cut at ``cuts``.

    The same cuts, ``letter`` / ``attachment`` kinds and discarded pages as
    :func:`confirm_slices`, for a PDF that is already a document (it arrived in
    an email). Parts stay in the document's batch, case and proceeding; each
    bundle's lead takes the document's place in its family (same parent, same
    sub-group) and the rest of the bundle become its enclosures. A part gets the
    source's reading of its own pages, so it is not OCR'd again. The source
    document and its derived rows are deleted. Raises :class:`SourceHasUserData`
    when that would lose work of the user, ``ValueError`` when the document
    cannot be split and :class:`SlicingFailed` when writing fails. Commits; the
    caller dispatches processing of the returned ids.
    """
    from app.services.document_service import DocumentService
    from app.services.pipeline_status import initialize as _pipeline_init

    slicing_meta = (doc.meta or {}).get("slicing", {})
    page_count = slicing_meta.get("page_count", 0)
    if not page_count:
        raise ValueError("Document slicing metadata missing")
    _, kept = _plan_parts(page_count, cuts, discard)
    if not doc.file_path:
        raise ValueError("Source PDF no longer available")
    pdf_path = resolve_storage_path(doc.file_path)
    if not pdf_path.exists():
        raise ValueError("Source PDF no longer available")
    reasons = user_data_reasons(db, doc)
    if reasons:
        raise SourceHasUserData(reasons)

    base_name = Path(doc.original_filename or "").stem or pdf_path.stem
    case_mark = (doc.extraction_confidence or {}).get("case_id")
    import pypdfium2 as pdfium

    new_docs: list[Document] = []
    written_slice_paths: list[Path] = []
    try:
        src_pdf = pdfium.PdfDocument(str(pdf_path))
        bundles: list[list[Document]] = []
        for part_idx, (page_indices, opens_letter) in enumerate(kept):
            slice_pdf = pdfium.PdfDocument.new()
            slice_pdf.import_pages(src_pdf, page_indices)
            # The document's folder is shared with other files: name by source.
            part_path = pdf_path.parent / f"{pdf_path.stem}_part{part_idx + 1}.pdf"
            slice_pdf.save(str(part_path))
            slice_pdf.close()
            written_slice_paths.append(part_path)

            pages = [p + 1 for p in page_indices]
            meta: dict = {"slice_range": [pages[0], pages[-1]], "pages": pages}
            prefilled = page_text.extraction_for_pages(doc, pages)
            if prefilled is not None:
                meta["prefilled_extraction"] = prefilled
            part = Document(
                title=f"{base_name} – Part {part_idx + 1}",
                owner_id=doc.owner_id,
                file_path=to_storage_path(part_path),
                original_filename=part_path.name,
                content_hash=hashlib.sha256(part_path.read_bytes()).hexdigest(),
                case_id=doc.case_id,
                proceeding_id=doc.proceeding_id,
                ingest_batch_id=doc.ingest_batch_id,
                internal_id=doc.internal_id,
                received_date=doc.received_date,
                meta=meta,
                page_count=len(pages),
            )
            if family_context.has_case(part):
                family_context.mark_case(
                    part, "user_set" if case_mark == "user_set" else "inherited"
                )
            db.add(part)
            db.flush()
            _pipeline_init(part, batched=doc.ingest_batch_id is not None, db=db)
            new_docs.append(part)
            if part_idx == 0 or opens_letter:
                bundles.append([])
            bundles[-1].append(part)
        src_pdf.close()

        _wire_document_bundles(db, doc, bundles)
    except Exception as exc:
        _discard_parts(db, written_slice_paths)
        raise SlicingFailed(f"Slicing failed: {exc}") from exc

    source_id = doc.id
    failure: Exception | None = None
    try:
        # Cascades the source's chunks, stages, claim evidence and edges away
        # and commits the parts with it. Its folder-mates (the other
        # attachments of the email) stay.
        removed = DocumentService(db).delete_document(source_id)
    except Exception as exc:
        removed = False
        failure = exc
    if not removed:
        db.rollback()
        if db.get(Document, source_id) is not None:
            _discard_parts(db, written_slice_paths)
            raise SlicingFailed(
                f"Slicing failed: {failure or 'the source could not be removed'}"
            ) from failure
        # The delete committed and only its aftermath failed: the parts stand.

    thumbs = (
        slicing_meta.get("pages")
        and Path(slicing_meta["pages"][0].get("thumbnail_path", "")).parent
    )
    if thumbs and thumbs.name.endswith("_thumbs"):
        shutil.rmtree(thumbs, ignore_errors=True)
    return [d.id for d in new_docs]


def _discard_parts(db: Session, paths: list[Path]) -> None:
    """Undo an uncommitted split: roll back and remove the part files written."""
    db.rollback()
    for path in paths:
        if path.exists():
            try:
                path.unlink()
            except OSError:
                pass


def _wire_document_bundles(
    db: Session, source: Document, bundles: list[list[Document]]
) -> None:
    """Family structure of the parts that replace ``source``.

    A bundle's lead stands where the source stood: under the source's parent
    (when it has one) and in its sub-group. Without a parent the first lead is
    cover letter or standalone, like a scan.
    """
    parent_id = source.parent_id
    parent = db.get(Document, parent_id) if parent_id is not None else None
    batch_id = source.ingest_batch_id
    group_id = source.sub_group_id or (parent.sub_group_id if parent else None)
    if (
        group_id is None
        and batch_id is not None
        and (parent is not None or len(bundles) > 1 or any(len(b) > 1 for b in bundles))
    ):
        # The family is now the user's structure (it keeps batch analysis from
        # reassigning roles), so it holds the parent too.
        group = BatchSubGroup(batch_id=batch_id, label=None, sort_order=0)
        db.add(group)
        db.flush()
        group_id = group.id
        if parent is not None:
            parent.sub_group_id = group_id
            parent.sub_group_sort_order = 0
    next_pos = 0
    if group_id is not None:
        db.flush()
        top = (
            db.query(func.max(Document.sub_group_sort_order))
            .filter(Document.sub_group_id == group_id, Document.id != source.id)
            .scalar()
        )
        next_pos = 0 if top is None else top + 1
    for members in bundles:
        lead, attachments = members[0], members[1:]
        if parent_id is not None:
            lead.parent_id = parent_id
            lead.role = DocumentRole.ENCLOSURE
            insert_edge_if_absent(
                db,
                from_document_id=parent_id,
                to_document_id=lead.id,
                relationship_type=RelationshipType.ENCLOSES,
                confidence=RelationshipConfidence.USER_CREATED,
                notes="split from an attachment",
            )
        else:
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
                notes="split: attachment",
            )
        if group_id is not None:
            for doc in members:
                doc.sub_group_id = group_id
                doc.sub_group_sort_order = next_pos
                next_pos += 1
