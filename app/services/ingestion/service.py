import hashlib
import os
import re
from datetime import UTC, datetime

import aiofiles
from celery.exceptions import SoftTimeLimitExceeded
from fastapi import HTTPException, UploadFile
from sqlalchemy.orm import Session

from app.config import DATA_DIR
from app.core.paths import resolve_storage_path, to_storage_path
from app.core.validators import validate_case_id
from app.models.database import (
    Case,
    Document,
    OriginatorType,
    User,
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
    extract_internal_id,
    extract_issued_date,
    extract_sender,
)

_H1_MIN_ALPHA_RATIO = 0.35


def _h1_looks_clean(text: str) -> bool:
    """Heuristic: reject H1 candidates that look like OCR garbage.

    Docling sometimes emits stylized PDF text (stamps, decorative
    letterheads, signatures) as H1 markdown headings. Tesseract OCR on
    those produces strings heavy in apostrophes, backslashes, angle
    brackets, etc., with very few alphabetic characters — e.g. doc 98's
    `--fr'lt"l\\ 'l- 4.- .//'tj<'-\\ z't/` at 29% alpha.

    Threshold tuned against real samples: garbage clocks ~30% alpha;
    even the most punctuation-heavy legit titles (Aktenzeichen + dates)
    stay above 40%. 35% gives some margin without over-rejecting.

    Rejected H1s fall through to extract_clean_title(filename, ""),
    which produces a more useful placeholder until METADATA's AI pass
    sets the real title."""
    if not text or len(text) < 3:
        return False
    alpha = sum(1 for c in text if c.isalpha())
    return alpha / len(text) >= _H1_MIN_ALPHA_RATIO


class IngestionError(Exception):
    """Structured error for ingestion pipeline failures."""

    def __init__(self, message: str, detail: str | None = None) -> None:
        self.message = message
        self.detail = detail
        super().__init__(self.message)


def _unique_upload_path(directory, filename: str) -> str:
    """Return a non-existing path in directory while preserving the display name."""
    filename = os.path.basename(filename)
    candidate = directory / filename
    if not candidate.exists():
        return str(candidate)
    stem, suffix = os.path.splitext(filename)
    counter = 1
    while True:
        candidate = directory / f"{stem}_{counter}{suffix}"
        if not candidate.exists():
            return str(candidate)
        counter += 1


def create_manual_upload_batch(
    db: Session,
    filenames: list[str],
    case_id: str | None = None,
    owner_id: int | None = None,
) -> int:
    """Create an IngestBatch row for one upload action.

    A single user upload = one batch, even if it contains multiple files.
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
        owner_id=owner_id,
    )
    batch.status = IngestBatchStatus.PROCESSING
    db.flush()
    return batch.id


def compute_review_reasons(doc: Document) -> list[str]:
    """Compute reasons why document needs review.

    'pending_confirmation' is the master flag that ensures human eyes
    always see the document at least once; it holds until the user confirms
    (`doc.confirmed_at`).
    """
    reasons = []

    # 1. Mandatory Human Confirmation
    if doc.confirmed_at is None:
        reasons.append("pending_confirmation")

    # 2. Structural Missing Data
    if not doc.case_id or doc.case_id == "_TRIAGE":
        reasons.append("missing_case_id")

    if not doc.originator_type:
        reasons.append("missing_originator")

    if not doc.sender:
        reasons.append("missing_sender")

    if not doc.received_date:
        reasons.append("missing_received_date")

    if not doc.issued_date:
        reasons.append("missing_issued_date")

    if doc.role == DocumentRole.ENCLOSURE and not doc.parent_id:
        reasons.append("missing_parent")

    # 3. Extraction Confidence
    conf = doc.extraction_confidence or {}
    # If any primary field is low/medium confidence, flag it
    for field in [
        "internal_id",
        "sender",
        "issued_date",
        "originator_type",
    ]:
        if conf.get(field) in ("low", "medium"):
            reasons.append("low_confidence")
            break

    # 4. Intelligence Flags
    try:
        from sqlalchemy import inspect

        from app.models.database import (
            Claim,
            ClaimEvidenceProposal,
            DocumentRelationship,
        )
        from app.models.enums import (
            ClaimEvidenceRole,
            ProposalStatus,
            RelationshipConfidence,
            RelationshipType,
        )
        from app.services.ingestion.plausibility import issued_date_suspect

        db = inspect(doc).session
        if db:
            if issued_date_suspect(doc, db):
                reasons.append("issued_date_suspect")

            # ENCLOSES edges come from the bundle layout (cover letter and its
            # enclosures), not from an AI judgement the user could confirm.
            unconfirmed = (
                db.query(DocumentRelationship)
                .filter(
                    DocumentRelationship.from_document_id == doc.id,
                    DocumentRelationship.confidence
                    == RelationshipConfidence.AI_DETECTED,
                    DocumentRelationship.relationship_type != RelationshipType.ENCLOSES,
                )
                .first()
            )
            if unconfirmed:
                reasons.append("unresolved_relationship")

            # The extractor only *proposes* evidence links; the flag holds
            # until the user confirms or dismisses each CONTESTS/REFUTES one.
            contested = (
                db.query(ClaimEvidenceProposal)
                .join(Claim, Claim.id == ClaimEvidenceProposal.target_claim_id)
                .filter(
                    Claim.dismissed_at.is_(None),
                    ClaimEvidenceProposal.source_document_id == doc.id,
                    ClaimEvidenceProposal.proposed_role.in_(
                        [ClaimEvidenceRole.CONTESTS, ClaimEvidenceRole.REFUTES]
                    ),
                    ClaimEvidenceProposal.status == ProposalStatus.PENDING,
                )
                .first()
            )
            if contested:
                reasons.append("contests_existing_claim")
    except Exception:
        pass

    # Hook for AI-detected contradictions (requires AI to populate this in doc.meta)
    if doc.meta and doc.meta.get("ai_contradiction"):
        reasons.append("contradiction_detected")

    # Pages whose OCR text a second engine could not corroborate (set by the
    # chandra extractor's cross-check).
    # Acknowledging moves the pages to ``ocr_unverified_acknowledged`` (see the
    # acknowledge route); that separate key is what clears this reason, so do not
    # fold the two together.
    if doc.meta and doc.meta.get("ocr_unverified_pages"):
        reasons.append("ocr_unverified")

    return list(dict.fromkeys(reasons))


def apply_review_reasons(doc: Document) -> None:
    """Recompute `review_reasons` and derive `needs_review` from them.

    The single writer for both fields. `missing_parent` is informational (the
    enclosure is still usable), so it alone doesn't hold a document in review.
    """
    reasons = compute_review_reasons(doc)
    doc.review_reasons = reasons
    doc.needs_review = bool(set(reasons) - {"missing_parent"})


def refresh_review_reasons(doc: Document, db, *, commit: bool = True) -> None:
    """Recompute and persist `review_reasons` / `needs_review` for one document.

    Pass commit=False when called inside a larger transaction that will commit later.

    RELATIONSHIPS and CLAIMS finish concurrently and each does a full recompute,
    so the standalone (commit=True) path locks the row and re-reads it first: the
    later writer then sees the other stage's committed edges/evidence instead of
    overwriting its reason with a stale snapshot.
    """
    if commit:
        db.query(Document).filter(
            Document.id == doc.id
        ).with_for_update().populate_existing().one()
    apply_review_reasons(doc)
    if commit:
        db.commit()


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

    # Match German amounts in two layouts:
    #   suffix: "1.234,56 EUR" / "500,00 €"  (lawyer invoices, modern court letters)
    #   prefix: "EUR 583,40" / "€ 1.234,56"  (RVG/GKG cost decisions, traditional)
    suffix_pattern = r"(?:\||^|\s+)(\d{1,3}(?:\.\d{3})*(?:,\d{2})?)\s*(?:EUR|€|euros?)"
    prefix_pattern = r"(?:EUR|€|euros?)\s*(\d{1,3}(?:\.\d{3})*(?:,\d{2})?)"

    matches: list[tuple[int, int, str]] = []
    for match in re.finditer(suffix_pattern, text, re.IGNORECASE):
        matches.append((match.start(), match.end(), match.group(1)))
    for match in re.finditer(prefix_pattern, text, re.IGNORECASE):
        matches.append((match.start(), match.end(), match.group(1)))

    for match_start, match_end, amount_raw in matches:
        amount_str = amount_raw.replace(".", "").replace(",", ".")
        try:
            amount = float(amount_str)
            if 10 < amount < 1000000:
                context = text[max(0, match_start - 50) : match_end + 50]
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


def _record_ocr_run(
    doc: Document, conversion_metadata: dict | None, markdown_content: str | None
) -> None:
    """Append a runs.jsonl entry for a Chandra OCR attempt, success or failure.

    ``convert_file`` never raises on a Chandra failure — it silently falls
    back to Docling (see converters.py) — so the failure signal is a note
    stashed on the fallback result's metadata (``chandra_ocr_error``), not an
    exception. No-ops when Chandra wasn't attempted (engine != chandra, or
    non-PDF), since Docling makes no model call to record.
    """
    if not conversion_metadata:
        return
    from app.services.ai_run_index import record_run

    if conversion_metadata.get("extractor") == "chandra-ocr-2":
        record_run(
            kind="doc",
            scope_id=str(doc.id),
            stage="ocr",
            doc_id=doc.id,
            batch_id=doc.ingest_batch_id,
            case_id=doc.case_id,
            model=conversion_metadata.get("ocr_model") or "",
            provider="chandra",
            duration_ms=int(conversion_metadata.get("extraction_seconds", 0) * 1000),
            response_len=len(markdown_content or ""),
            status="ok",
        )
    elif conversion_metadata.get("chandra_ocr_error"):
        record_run(
            kind="doc",
            scope_id=str(doc.id),
            stage="ocr",
            doc_id=doc.id,
            batch_id=doc.ingest_batch_id,
            case_id=doc.case_id,
            model=conversion_metadata.get("chandra_ocr_model") or "",
            provider="chandra",
            # Chandra's own duration isn't surfaced through the Docling
            # fallback path — only the failure message is.
            duration_ms=0,
            status="error",
            error=conversion_metadata["chandra_ocr_error"][:200],
        )


def process_uploaded_document(doc: Document, db: Session):
    """Process a pending document in the background."""
    import os

    if not doc.file_path:
        raise IngestionError("Document has no file_path")
    file_path = str(resolve_storage_path(doc.file_path))
    if not os.path.exists(file_path):
        raise IngestionError(f"File not found: {file_path}")

    ext = os.path.splitext(file_path)[1].lower()
    safe_filename = os.path.basename(file_path)

    markdown_content: str | None = None
    conversion_metadata: dict | None = None
    conversion_error: str | None = None

    try:
        from app.services.user_settings_service import get_extraction_engine

        engine = get_extraction_engine(db)
        conversion_result = convert_file(file_path, engine=engine)
        markdown_content = conversion_result["content"]
        conversion_metadata = conversion_result["metadata"]
        # Add chunks to metadata
        conversion_metadata["chunks"] = conversion_result.get("chunks", [])

        _record_ocr_run(doc, conversion_metadata, markdown_content)

        if ext == ".pdf" and markdown_content:
            text_only = re.sub(r"[#*_\[\]()]|!\[.*?\]\(.*?\)", "", markdown_content)
            text_only = re.sub(r"\n+", "\n", text_only).strip()
            if len(text_only) < 100:
                import logging

                logging.getLogger(__name__).warning(
                    f"Document '{safe_filename}' extracted to only {len(text_only)} chars - possible scanned document"
                )

    except SoftTimeLimitExceeded:
        # An Exception subclass — must come before the generic branch below,
        # or process_document_task would see an IngestionError instead of
        # SoftTimeLimitExceeded and treat a soft time limit as an ordinary
        # (retryable-looking) conversion failure rather than its own
        # dedicated no-retry, cascade-and-return-cleanly branch.
        raise
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
    h1_text = h1.group(1).strip() if h1 else ""
    if h1_text and len(h1_text) < 100 and _h1_looks_clean(h1_text):
        doc.title = h1_text
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
    result_date = extract_issued_date(content, safe_filename)
    result_sender = extract_sender(content)
    result_internal_id = extract_internal_id(content)
    if result_case_id["value"]:
        # Same guard as ingest_file's filename-sniff check: a case id
        # sniffed from the filename or body content (Aktenzeichen etc.) must
        # not silently move a document into a case its owner can't edit —
        # this runs during background processing, after the upload-time
        # check already applied, so it needs the same protection.
        from app.services import access_service

        target_case = db.query(Case).filter(Case.id == result_case_id["value"]).first()
        owner = db.get(User, doc.owner_id) if doc.owner_id is not None else None
        if access_service.can_edit_case(db, owner, target_case):
            doc.case_id = result_case_id["value"]

    # Fields the user set in triage ("user_set") are not the extractor's to redo.
    conf = dict(doc.extraction_confidence or {})
    if conf.get("sender") != "user_set":
        doc.sender = result_sender["value"]
    if conf.get("issued_date") != "user_set":
        doc.issued_date = result_date["value"]
    if not doc.received_date:
        doc.received_date = datetime.now(UTC)
    if result_internal_id["value"] and not doc.internal_id:
        doc.internal_id = result_internal_id["value"]
    extracted = ExtractionConfidenceSchema(
        sender=result_sender["confidence"],
        issued_date=result_date["confidence"],
        internal_id=result_internal_id["confidence"],
    ).model_dump()
    doc.extraction_confidence = {
        **conf,
        **{k: v for k, v in extracted.items() if conf.get(k) != "user_set"},
    }

    apply_review_reasons(doc)


def _create_document(
    db: Session,
    file_path: str,
    content_hash: str,
    case_id: str,
    safe_filename: str,
    parent_id: int | None,
    ingest_batch_id: int | None,
    original_filename: str | None = None,
    owner_id: int | None = None,
) -> Document:
    """Create the Document row for an upload; extraction happens in the pipeline."""
    from app.services.pipeline_status import initialize as _pipeline_init

    extracted_title = extract_clean_title(safe_filename, "")

    new_doc = Document(
        title=extracted_title if extracted_title != safe_filename else safe_filename,
        owner_id=owner_id,
        case_id=case_id,
        file_path=file_path,
        original_filename=original_filename or safe_filename,
        content_hash=content_hash,
        parent_id=parent_id,
        originator_type=OriginatorType.UNKNOWN,
        cost_candidates=[],
        ingest_batch_id=ingest_batch_id,
    )

    db.add(new_doc)
    db.flush()
    _pipeline_init(new_doc, batched=ingest_batch_id is not None, db=db)

    apply_review_reasons(new_doc)

    return new_doc


async def ingest_file(
    file: UploadFile,
    case_id: str | None = None,
    db: Session | None = None,
    parent_id: int | None = None,
    ingest_batch_id: int | None = None,
    owner_id: int | None = None,
) -> Document:
    """Save an uploaded file and create its Document; the pipeline processes it."""
    if db is None:
        raise ValueError("ingest_file requires a database session.")
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
        if preliminary_case_id != "_TRIAGE":
            validated = validate_case_id(preliminary_case_id)
            if not validated:
                raise HTTPException(
                    status_code=400,
                    detail=f"Invalid case_id '{preliminary_case_id}'.",
                )
            preliminary_case_id = validated
            # The caller's own case_id was already access-checked by the route
            # (see the upload API (app/api/v1/upload.py)). A case_id sniffed from the filename
            # was not — without this, naming a file after another user's case
            # id would silently file it there. Fall back to _TRIAGE rather than
            # erroring, since filename-sniffing is a best-effort convenience,
            # not a hard requirement.
            if not case_id and owner_id is not None:
                from app.services import access_service

                target_case = (
                    db.query(Case).filter(Case.id == preliminary_case_id).first()
                )
                owner = db.get(User, owner_id)
                if not access_service.can_edit_case(db, owner, target_case):
                    preliminary_case_id = "_TRIAGE"
        if preliminary_case_id == "_TRIAGE" and ingest_batch_id is not None:
            case_dir = DATA_DIR / "_TRIAGE" / f"ib-{ingest_batch_id}"
        else:
            case_dir = DATA_DIR / preliminary_case_id
        if (
            DATA_DIR.resolve() not in case_dir.resolve().parents
            and case_dir.resolve() != DATA_DIR.resolve()
        ):
            raise HTTPException(
                status_code=400,
                detail="case_id resolves outside the data directory.",
            )
        case_dir.mkdir(parents=True, exist_ok=True)
        file_path = _unique_upload_path(case_dir, safe_filename)

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
            # The only HTTPException reachable inside this block is the 413
            # above — the file being written this call is now orphaned
            # (partial content, no Document row yet), so clean it up before
            # propagating. Scoped to this block only; the 400/409 paths
            # later in this function already do their own explicit cleanup.
            # Same file_path used unguarded by the sibling os.remove calls
            # later in this function (magic-mismatch / duplicate paths,
            # dismissed CodeQL alerts #38/#39) — case_dir is validated under
            # DATA_DIR above and file_path's filename component was already
            # os.path.basename()'d inside _unique_upload_path (dismissed
            # alerts #32/#33); CodeQL doesn't model either barrier.
            if os.path.exists(file_path):
                os.remove(file_path)
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

        dup_query = db.query(Document).filter(
            Document.content_hash == content_hash,
            Document.case_id == preliminary_case_id,
        )
        if preliminary_case_id == "_TRIAGE":
            # _TRIAGE is a shared bucket across every user — without this,
            # the 409 below would leak another user's document title/id.
            # A real case_id doesn't need this: reaching one here already
            # means the uploader has edit access to it (see the guard
            # above), so seeing its other docs isn't a new disclosure.
            dup_query = dup_query.filter(Document.owner_id == owner_id)
        existing = dup_query.first()
        if existing:
            os.remove(file_path)
            raise HTTPException(
                status_code=409,
                detail=f"Duplicate document: '{existing.title}' (ID: {existing.id})",
            )

        new_doc = _create_document(
            db=db,
            file_path=to_storage_path(file_path),
            content_hash=content_hash,
            case_id=preliminary_case_id,
            safe_filename=safe_filename,
            parent_id=parent_id,
            ingest_batch_id=ingest_batch_id,
            original_filename=file.filename,
            owner_id=owner_id,
        )
        db.add(new_doc)
        db.flush()
        db.commit()
        db.refresh(new_doc)
        return new_doc

    except HTTPException:
        raise
    except Exception:
        if file_path and os.path.exists(file_path):
            try:
                os.remove(file_path)
            except OSError:
                pass
        raise
