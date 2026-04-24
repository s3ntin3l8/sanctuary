import hashlib
import os
import re

import aiofiles
from fastapi import HTTPException, UploadFile
from sqlalchemy.orm import Session

from app.config import DATA_DIR
from app.models.database import (
    Document,
    OriginatorType,
)
from app.models.enums import DocumentRole, IngestBatchSourceType, IngestBatchStatus
from app.models.schemas import (
    CostCandidateSchema,
    ExtractionConfidenceSchema,
)
from app.repositories.ingest_batch import IngestBatchRepository
from app.services.ingestion.converters import (
    ALLOWED_EXTENSIONS,
    MAX_FILE_SIZE,
    convert_file,
    is_valid_docling_output,
    validate_file_magic,
)
from app.services.ingestion.extractors import (
    extract_case_id,
    extract_originator,
    extract_received_date,
    extract_sender,
)


class IngestionError(Exception):
    """Structured error for ingestion pipeline failures."""

    def __init__(self, message: str, detail: str | None = None) -> None:
        self.message = message
        self.detail = detail
        super().__init__(self.message)


def create_manual_upload_batch(
    db: Session,
    filenames: list[str],
    case_id: str | None = None,
) -> int:
    """Create an IngestBatch row for one upload action.

    A single user upload = one batch, even if it contains multiple files.
    Email/scan batches come later (Phase 3).
    """
    subject = (
        filenames[0]
        if len(filenames) == 1
        else f"{len(filenames)} files ({filenames[0]}, ...)"
    )
    repo = IngestBatchRepository(db)
    batch = repo.create_batch(
        source_type=IngestBatchSourceType.MANUAL,
        subject=subject,
        case_id=case_id,
    )
    batch.status = IngestBatchStatus.PROCESSING
    db.flush()
    return batch.id


def compute_review_reasons(doc: Document) -> list[str]:
    """Compute reasons why document needs review."""
    reasons = []

    if not doc.case_id or doc.case_id == "_TRIAGE":
        reasons.append("missing_case_id")

    if not doc.originator_type:
        reasons.append("missing_originator")

    if not doc.sender:
        reasons.append("missing_sender")

    if not doc.received_date:
        reasons.append("missing_received_date")

    if doc.role == DocumentRole.ENCLOSURE and not doc.parent_id:
        reasons.append("missing_parent")

    return reasons


def extract_clean_title(filename: str, content: str = "") -> str:
    """Extract clean title from filename."""
    import re

    name = filename.rsplit(".", 1)[0] if "." in filename else filename
    name = re.sub(r"[-_]+", " ", name)
    name = re.sub(r"\s+", " ", name).strip()

    if content:
        match = re.search(r"^#\s+(.+)$", content, re.MULTILINE)
        if match:
            title = match.group(1).strip()
            if len(title) < 100:
                return title

    return name


def extract_cost_candidates(content: str) -> list[dict]:
    """Extract cost candidates from content."""
    import re

    candidates = []
    text = content[:10000] if content else ""

    # Pattern to match amounts like "1.234,56 EUR" or "500,00 €"
    # Added allowance for table pipes | and extra whitespace around the amount
    amount_pattern = r"(?:\||^|\s+)(\d{1,3}(?:\.\d{3})*(?:,\d{2})?)\s*(?:EUR|€|euros?)"

    for match in re.finditer(amount_pattern, text, re.IGNORECASE):
        amount_str = match.group(1).replace(".", "").replace(",", ".")
        try:
            amount = float(amount_str)
            if 10 < amount < 1000000:
                context = text[max(0, match.start() - 50) : match.end() + 50]
                candidates.append(
                    {
                        "type": "amount",
                        "value": amount,
                        "context": context,
                    }
                )
        except ValueError:
            pass

    rvg_pattern = (
        r"(?:Nr\.?\s*)?(\d{1,4}\s*VV\s*RVG|KV\s*GKG\s*Nr\.?\s*\d+|§\s*\d+\s*ZPO)"
    )
    for match in re.finditer(rvg_pattern, text, re.IGNORECASE):
        candidates.append(
            {
                "type": "rvg_position",
                "value": match.group(1).strip(),
                "context": text[max(0, match.start() - 30) : match.end() + 30],
            }
        )

    return candidates[:20]


def process_uploaded_document(doc: Document, db: Session):
    """Process a pending document in the background."""
    import os

    file_path = doc.file_path
    if not file_path or not os.path.exists(file_path):
        raise IngestionError(f"File not found: {file_path}")

    ext = os.path.splitext(file_path)[1].lower()
    safe_filename = os.path.basename(file_path)

    markdown_content: str | None = None
    conversion_metadata: dict | None = None
    conversion_error: str | None = None

    try:
        conversion_result = convert_file(file_path)
        markdown_content = conversion_result["content"]
        conversion_metadata = conversion_result["metadata"]
        # Add chunks to metadata
        conversion_metadata["chunks"] = conversion_result.get("chunks", [])

        if ext == ".pdf" and markdown_content:
            text_only = re.sub(r"[#*_\[\]()]|!\[.*?\]\(.*?\)", "", markdown_content)
            text_only = re.sub(r"\n+", "\n", text_only).strip()
            if len(text_only) < 100:
                import logging

                logging.getLogger(__name__).warning(
                    f"Document '{safe_filename}' extracted to only {len(text_only)} chars - possible scanned document"
                )

    except Exception as e:
        error_str = str(e)
        if "timed out" in error_str.lower():
            conversion_error = f"OCR timeout: {error_str}"
        else:
            conversion_error = error_str
        markdown_content = f"Conversion failed: {conversion_error}"
        raise IngestionError(f"Conversion error: {e}") from e

    if not is_valid_docling_output(markdown_content):
        raise IngestionError(
            "Docling conversion failed or produced empty content.",
            detail=conversion_error,
        )

    doc.content = markdown_content
    # Prefer H1 from content; fall back to filename-derived title only when no
    # title has been set yet (e.g. subject of an email body doc).
    h1 = re.search(r"^#\s+(.+)$", markdown_content or "", re.MULTILINE)
    if h1 and len(h1.group(1).strip()) < 100:
        doc.title = h1.group(1).strip()
    elif not doc.title:
        doc.title = extract_clean_title(safe_filename, "")
    doc.meta = conversion_metadata

    _apply_script_extractors(doc, markdown_content or "", db)

    raw_costs = extract_cost_candidates(markdown_content or "")
    doc.cost_candidates = [
        CostCandidateSchema(**c).model_dump() for c in raw_costs if isinstance(c, dict)
    ]

    db.commit()


def _apply_script_extractors(doc: Document, content: str, db: Session) -> None:
    """Run the 4 regex extractors and apply their results to doc in-place.

    Also refreshes review_reasons/needs_review since extraction results affect them.
    """
    safe_filename = os.path.basename(doc.file_path) if doc.file_path else ""

    result_case_id = extract_case_id(safe_filename, content)
    result_originator = extract_originator(safe_filename, content)
    result_date = extract_received_date(content, safe_filename)
    result_sender = extract_sender(content)

    if result_case_id["value"]:
        from app.models.database import Case as CaseModel

        if db.query(CaseModel).filter(CaseModel.id == result_case_id["value"]).first():
            doc.case_id = result_case_id["value"]

    doc.originator_type = result_originator["value"]
    doc.sender = result_sender["value"]
    doc.received_date = result_date["value"]
    doc.extraction_confidence = ExtractionConfidenceSchema(
        sender=result_sender["confidence"],
        date=result_date["confidence"],
        originator=result_originator["confidence"],
    ).model_dump()

    reasons = compute_review_reasons(doc)
    doc.review_reasons = reasons
    doc.needs_review = len(reasons) > 0


def _create_document(
    db: Session,
    file_path: str,
    content_hash: str,
    case_id: str,
    safe_filename: str,
    parent_id: int | None,
    ingest_batch_id: int | None,
    content: str | None = None,
    markdown_content: str | None = None,
    conversion_metadata: dict | None = None,
) -> Document:
    """Shared Document creation logic - reduces duplication between skip_processing paths."""
    from app.services.pipeline_status import initialize as _pipeline_init

    if markdown_content:
        extracted_title = extract_clean_title(safe_filename, markdown_content)
    else:
        extracted_title = extract_clean_title(safe_filename, "")

    new_doc = Document(
        title=extracted_title if extracted_title != safe_filename else safe_filename,
        content=content or markdown_content,
        case_id=case_id,
        file_path=file_path,
        content_hash=content_hash,
        parent_id=parent_id,
        originator_type=OriginatorType.UNKNOWN,
        cost_candidates=extract_cost_candidates(markdown_content or "")
        if markdown_content
        else [],
        meta=conversion_metadata,
        ingest_batch_id=ingest_batch_id,
    )

    _pipeline_init(new_doc, batched=ingest_batch_id is not None)

    reasons = compute_review_reasons(new_doc)
    new_doc.review_reasons = reasons
    new_doc.needs_review = len(reasons) > 0

    return new_doc


async def ingest_file(
    file: UploadFile,
    case_id: str | None = None,
    db: Session = None,
    parent_id: int = None,
    skip_processing: bool = False,
    ingest_batch_id: int | None = None,
) -> Document:
    """Save uploaded file, optionally process it."""
    file_path: str | None = None

    try:
        if not file.filename:
            raise HTTPException(status_code=400, detail="No filename provided.")

        ext = os.path.splitext(file.filename)[1].lower()
        if ext not in ALLOWED_EXTENSIONS:
            allowed_ext_str = ", ".join(sorted(ALLOWED_EXTENSIONS))
            raise HTTPException(
                status_code=400,
                detail=f"Unsupported file type '{ext}'. Allowed: {allowed_ext_str}",
            )

        safe_filename = os.path.basename(file.filename)
        extracted_case_obj = extract_case_id(safe_filename, "")
        extracted_case_id = (
            extracted_case_obj.get("value")
            if isinstance(extracted_case_obj, dict)
            else None
        )
        preliminary_case_id = case_id if case_id else (extracted_case_id or "_TRIAGE")
        case_dir = DATA_DIR / preliminary_case_id
        case_dir.mkdir(parents=True, exist_ok=True)
        file_path = str(case_dir / safe_filename)

        sha256 = hashlib.sha256()
        try:
            async with aiofiles.open(file_path, "wb") as out_file:
                total_size = 0
                while content := await file.read(1024 * 1024):
                    total_size += len(content)
                    if total_size > MAX_FILE_SIZE:
                        max_mb = MAX_FILE_SIZE // (1024 * 1024)
                        raise HTTPException(
                            status_code=413,
                            detail=f"File too large. Maximum size: {max_mb}MB",
                        )
                    sha256.update(content)
                    await out_file.write(content)
        except HTTPException:
            raise
        except OSError as e:
            raise HTTPException(
                status_code=500,
                detail=f"Failed to save uploaded file: {e}",
            ) from e

        content_hash = sha256.hexdigest()

        magic_ext = validate_file_magic(file_path)
        # docx/pptx/xlsx are ZIP-based — magic returns ".zip" for all of them, which is correct
        _zip_based = {".docx", ".pptx", ".xlsx"}
        if (
            magic_ext
            and magic_ext != ext
            and not (magic_ext == ".zip" and ext in _zip_based)
        ):
            os.remove(file_path)
            raise HTTPException(
                status_code=400,
                detail=f"File content does not match extension. Expected {ext}, got {magic_ext}",
            )

        existing = (
            db.query(Document)
            .filter(
                Document.content_hash == content_hash,
                Document.case_id == preliminary_case_id,
            )
            .first()
        )
        if existing:
            os.remove(file_path)
            raise HTTPException(
                status_code=409,
                detail=f"Duplicate document: '{existing.title}' (ID: {existing.id})",
            )

        if skip_processing:
            new_doc = _create_document(
                db=db,
                file_path=file_path,
                content_hash=content_hash,
                case_id=preliminary_case_id,
                safe_filename=safe_filename,
                parent_id=parent_id,
                ingest_batch_id=ingest_batch_id,
                content=None,
            )
            db.add(new_doc)
            db.flush()
            db.commit()
            db.refresh(new_doc)
            return new_doc

        markdown_content: str | None = None
        conversion_metadata: dict | None = None
        conversion_error: str | None = None

        try:
            conversion_result = convert_file(file_path)
            markdown_content = conversion_result["content"]
            conversion_metadata = conversion_result["metadata"]
            conversion_metadata["chunks"] = conversion_result.get("chunks", [])
        except TimeoutError:
            conversion_error = "Conversion timed out after 60 seconds"
            markdown_content = f"Conversion failed: {conversion_error}"
        except Exception as e:
            conversion_error = str(e)
            markdown_content = f"Conversion failed: {conversion_error}"

        if not is_valid_docling_output(markdown_content):
            raise IngestionError(
                "Docling conversion failed or produced empty content.",
                detail=conversion_error,
            )

        new_doc = _create_document(
            db=db,
            file_path=file_path,
            content_hash=content_hash,
            case_id=preliminary_case_id,
            safe_filename=safe_filename,
            parent_id=parent_id,
            ingest_batch_id=ingest_batch_id,
            markdown_content=markdown_content,
            conversion_metadata=conversion_metadata,
        )
        _apply_script_extractors(new_doc, markdown_content or "", db)
        db.add(new_doc)
        db.flush()
        db.commit()
        db.refresh(new_doc)
        return new_doc

    except HTTPException:
        raise
    except IngestionError:
        raise
    except Exception:
        if file_path and os.path.exists(file_path):
            try:
                os.remove(file_path)
            except OSError:
                pass
        raise
