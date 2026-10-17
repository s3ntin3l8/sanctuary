import hashlib
import logging
import unicodedata
from collections.abc import Iterable
from datetime import datetime
from email.utils import parseaddr
from pathlib import Path

import pypdfium2 as pdfium
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from app.config import DATA_DIR
from app.core import secrets
from app.core.paths import to_storage_path
from app.core.private_files import write_private
from app.core.timezone import now_utc
from app.models.database import Document, IngestBatch
from app.models.enums import IngestBatchSourceType, IngestBatchStatus
from app.repositories.ingest_batch import (
    NO_NEW_DOCUMENTS,
    IngestBatchRepository,
    is_no_document_tombstone,
)
from app.services.ingestion.archive_guard import (
    ARCHIVE_EXTENSIONS,
    ArchiveRejected,
    check_zip,
)
from app.services.ingestion.email_parser import parse_rfc822
from app.services.ingestion.exhibits import assign_exhibits
from app.services.ingestion.extractors import (
    BEA_MANIFEST_CONFIDENCE,
    EMAIL_HEADER_CONFIDENCE,
    extract_az_court_from_subject,
    extract_internal_id_from_subject,
)
from app.tasks.dispatch import dispatch_task

logger = logging.getLogger(__name__)


def _resolve_owner_id(db: Session, owner_id: int | None) -> int | None:
    """Ingestion must never produce an unowned row. When the caller can't supply
    an owner (e.g. a legacy/root scan-folder file), fall back to the bootstrap
    admin so the document lands in *someone's* triage inbox."""
    if owner_id is not None:
        return owner_id
    try:
        from app.services import auth_service

        admin = auth_service.get_or_create_bootstrap_admin(db)
        return admin.id if admin is not None else None
    except Exception:  # pragma: no cover - defensive
        return None


def _sanitize_filename(name: str) -> str:
    """Sanitize filename while preserving Unicode characters (e.g. German umlauts)."""
    if not name:
        return "unnamed"
    name = unicodedata.normalize("NFC", name)
    safe_chars = "-_.() "
    result = "".join(c if c.isalnum() or c in safe_chars else "_" for c in name)
    return result.strip() or "unnamed"


def _try_assign_case_from_subject(
    db: Session, batch: IngestBatch, subject: str, owner_id: int | None
) -> None:
    case_id, proceeding_id = _resolve_subject_case(db, subject, owner_id)
    if case_id:
        # Ingest currently passes a fresh batch; also preserve an existing
        # proceeding when this helper is reused for the same case.
        if case_id != batch.case_id or proceeding_id is not None:
            batch.proceeding_id = proceeding_id
        batch.case_id = case_id
        logger.info(
            "Batch #%s: auto-assigned to case %s via subject", batch.id, case_id
        )


def _resolve_subject_case(
    db: Session, subject: str, owner_id: int | None
) -> tuple[str | None, int | None]:
    """Resolve the case and proceeding from the email subject when possible.

    Tries internal_id (lawyer's file number, e.g. '8372/25') first — it maps 1:1 to
    Case.id per CLAUDE.md.  Falls back to az_court (court Aktenzeichen) if present.
    Only returns matching DB rows; never creates records here.

    Auto-filing a batch into a case is a write to that case, so both paths
    are restricted to the owner's *editable* cases (owned ∪ EDITOR shares).
    The az_court path filters inside the query rather than checking after
    `.first()`: an Aktenzeichen is shared by both sides of the same lawsuit,
    so an unfiltered query could match a different user's Proceeding row
    first and shadow the owner's own legitimate match — a check-after-fetch
    would then just fail closed instead of finding the right one.
    """
    from app.models.database import Case, Proceeding, User
    from app.services import access_service

    internal_id = extract_internal_id_from_subject(subject)
    az_court = extract_az_court_from_subject(subject)

    owner = db.get(User, owner_id) if owner_id is not None else None
    editable = access_service.editable_case_ids(db, owner)

    if internal_id:
        case = db.query(Case).filter(Case.id == internal_id).first()
        if case and (editable is None or case.id in editable):
            proceeding_id = None
            if az_court:
                proc = (
                    db.query(Proceeding)
                    .filter(
                        Proceeding.case_id == case.id,
                        Proceeding.az_court == az_court,
                    )
                    .first()
                )
                if proc:
                    proceeding_id = proc.id
            return case.id, proceeding_id

    if az_court:
        proc_query = db.query(Proceeding).filter(Proceeding.az_court == az_court)
        if editable is not None:
            proc_query = proc_query.filter(Proceeding.case_id.in_(editable))
        proc = proc_query.first()
        if proc:
            return proc.case_id, proc.id
    return None, None


def _is_outgoing(sender: str, own_addresses: Iterable[str]) -> bool:
    own = {a.strip().lower() for a in own_addresses if a.strip()}
    return bool(own) and parseaddr(sender or "")[1].lower() in own


def ingest_raw_email(
    db: Session,
    raw_bytes: bytes,
    source_type: IngestBatchSourceType = IngestBatchSourceType.EMAIL,
    owner_id: int | None = None,
    own_addresses: Iterable[str] = (),
    gmail_id: str | None = None,
    gmail_thread_id: str | None = None,
    mailbox_received_at: datetime | None = None,
) -> IngestBatch | None:
    """Ingest one RFC822 email as a batch.

    ``own_addresses`` are the addresses the user sends from: mail from one of
    them is the user's own writing, so its body is kept even with attachments.
    """
    owner_id = _resolve_owner_id(db, owner_id)
    parsed = parse_rfc822(raw_bytes)
    msg_id = parsed["message_id"] or None
    sender = parsed["sender"] or "unknown"
    subject = parsed["subject"] or "No Subject"

    batch_repo = IngestBatchRepository(db)

    if gmail_id:
        existing_gmail = (
            db.query(IngestBatch)
            .filter(IngestBatch.owner_id == owner_id, IngestBatch.gmail_id == gmail_id)
            .first()
        )
        if existing_gmail:
            return None if is_no_document_tombstone(existing_gmail) else existing_gmail

    source_hash = None
    if msg_id:
        existing = batch_repo.get_by_message_id(msg_id, owner_id)
        if existing:
            doc_count = (
                db.query(Document)
                .filter(Document.ingest_batch_id == existing.id)
                .count()
            )
            if doc_count > 0:
                logger.info(
                    "Email duplicate: message-id %s already in batch #%d (%d docs) — skipping",
                    msg_id,
                    existing.id,
                    doc_count,
                )
                return existing
            if (
                existing.email_sources
                and existing.source_review_reasons
                and not is_no_document_tombstone(existing)
            ):
                return existing
            if is_no_document_tombstone(existing):
                logger.info(
                    "Email message-id %s already ingested as a no-document batch "
                    "#%d — skipping",
                    msg_id,
                    existing.id,
                )
                return None
            logger.info(
                "Email batch #%d has 0 docs (orphaned) — deleting and re-ingesting",
                existing.id,
            )
            db.delete(existing)
            db.flush()
    else:
        # Hash the raw email bytes directly — avoids collisions from emails with the
        # same sender/subject but empty bodies (T3.11).
        fallback_hash = hashlib.sha256(raw_bytes).hexdigest()
        existing = (
            db.query(IngestBatch)
            .filter(
                IngestBatch.source_type == IngestBatchSourceType.EMAIL,
                IngestBatch.source_hash == fallback_hash,
                IngestBatch.owner_id == owner_id,
            )
            .first()
        )
        if existing:
            doc_count = (
                db.query(Document)
                .filter(Document.ingest_batch_id == existing.id)
                .count()
            )
            if doc_count > 0:
                logger.info(
                    "Email duplicate (fallback hash): already in batch #%d (%d docs) — skipping",
                    existing.id,
                    doc_count,
                )
                return existing
            if (
                existing.email_sources
                and existing.source_review_reasons
                and not is_no_document_tombstone(existing)
            ):
                return existing
            if is_no_document_tombstone(existing):
                logger.info(
                    "Email (fallback hash) already ingested as a no-document "
                    "batch #%d — skipping",
                    existing.id,
                )
                return None
            db.delete(existing)
            db.flush()
            source_hash = fallback_hash
        else:
            source_hash = fallback_hash if not msg_id else None

    received_date = mailbox_received_at or parsed.get("received_date") or now_utc()

    batch = batch_repo.create_batch(
        source_type=source_type,
        owner_id=owner_id,
        subject=subject[:255],
        sender_email=sender[:255] if sender != "unknown" else None,
        received_at=received_date,
    )
    batch.message_id = msg_id
    # Kept for every email (attachments or not): the body doc that used to carry
    # these in meta["threading"] is discarded whenever attachments are present.
    batch.in_reply_to = parsed.get("in_reply_to")
    batch.thread_refs = parsed.get("thread_refs") or None
    if source_hash:
        batch.source_hash = source_hash

    batch.gmail_id = gmail_id
    batch.gmail_thread_id = gmail_thread_id
    batch.received_source = (
        "gmail"
        if mailbox_received_at
        else ("email_date" if parsed.get("received_date") else "ingest_time")
    )

    try:
        db.flush()
    except IntegrityError:
        # Two concurrent requests for the same email (e.g. Gmail sync racing
        # a manual re-sync) both passed the SELECT-based duplicate check
        # above before either committed — the new UNIQUE(owner_id,
        # message_id/source_hash) constraint is the real guard here. Losing
        # this race isn't an error: the winner's batch is the correct
        # result, so recover it and return that instead of propagating.
        db.rollback()
        existing = (
            batch_repo.get_by_message_id(msg_id, owner_id)
            if msg_id
            else batch_repo.get_by_source_hash(source_hash, owner_id)
            if source_hash
            else None
        )
        if existing is not None:
            logger.info(
                "Email batch race: lost to concurrent insert — reusing batch #%d",
                existing.id,
            )
            return existing
        raise

    # Attempt to auto-assign case from the email subject so downstream stages
    # receive a case_id/proceeding_id without waiting for AI metadata.
    _try_assign_case_from_subject(db, batch, subject, owner_id)

    logger.info(
        "Email batch #%d created: from=%s subject=%r attachments=%d",
        batch.id,
        sender,
        subject,
        len(parsed["attachments"]),
    )

    case_dir = DATA_DIR / "_TRIAGE"
    case_dir.mkdir(parents=True, exist_ok=True)
    # Attachment paths written here initially — SQLAlchemy event moves them to
    # the case/proceeding folder once confirmed.

    docs_to_process: list[Document] = []
    has_attachments = bool(parsed["attachments"])
    # Paths written below — on any exception before the commit at the end of
    # this try block, these (and only these; a dedup-reused attachment's
    # existing file is never appended) are removed so the DB rollback
    # doesn't leave orphaned files with no row pointing at them.
    written_paths: list[Path] = []

    try:
        raw_path = DATA_DIR / "email_sources" / str(owner_id) / f"{batch.id}.eml"
        write_private(raw_path, secrets.encrypt_bytes(raw_bytes))
        written_paths.append(raw_path)
        batch.raw_source_path = to_storage_path(raw_path)
        _ingest_email_docs_and_commit(
            db,
            batch,
            parsed,
            subject,
            sender,
            owner_id,
            received_date,
            case_dir,
            has_attachments,
            own_addresses,
            docs_to_process,
            written_paths,
        )
    except Exception:
        db.rollback()
        for p in written_paths:
            if p.exists():
                try:
                    p.unlink()
                except OSError:
                    pass
        raise

    if not docs_to_process:
        return None if is_no_document_tombstone(batch) else batch

    logger.info(
        "Batch #%d committed — dispatching process_document_task for %d doc(s)",
        batch.id,
        len(docs_to_process),
    )
    for doc in docs_to_process:
        dispatch_task("app.tasks.document_processing.process_document_task", doc.id)

    return batch


def _ingest_email_docs_and_commit(
    db: Session,
    batch: IngestBatch,
    parsed: dict,
    subject: str,
    sender: str,
    owner_id: int | None,
    received_date,
    case_dir: Path,
    has_attachments: bool,
    own_addresses: Iterable[str],
    docs_to_process: list[Document],
    written_paths: list[Path],
) -> None:
    """Body/attachment document creation and the final commit for
    ingest_raw_email — split out purely so the caller can wrap every raise
    point between here and the commit in one try/except for file cleanup."""
    from app.services.ingestion.converters import (
        ALLOWED_EXTENSIONS,
        MAX_FILE_SIZE,
        magic_extension,
        same_format,
    )
    from app.services.ingestion.email_sources import bind_manifests, persist_sources
    from app.services.pipeline_status import initialize as _pipeline_init

    auto_replied = bool(parsed.get("auto_replied"))
    if auto_replied:
        batch.meta = {**(batch.meta or {}), "skipped": "auto_replied"}
    for source in parsed["sources"]:
        if auto_replied:
            # Out-of-office notices carry no evidence: record the message as
            # handled (a tombstone) so it is not offered or fetched again.
            for att in source["attachments"]:
                att.update(outcome="removed", reason="Automatic reply")
            continue
        source_sender = source["sender"]
        source_subject = source["subject"] or subject
        # Resolve an attached message's own reference before inheriting the
        # outer forwarding subject. Never overwrite the outer batch's identity.
        source_case, source_proceeding = batch.case_id, batch.proceeding_id
        if source["parent_id"] is not None:
            inner_case, inner_proceeding = _resolve_subject_case(
                db, source_subject, owner_id
            )
            if inner_case:
                source_case, source_proceeding = inner_case, inner_proceeding
        if source["body"].strip() and (
            not source["has_attachments"] or _is_outgoing(source_sender, own_addresses)
        ):
            name = (
                f"email_body_{batch.id}.txt"
                if source["id"] == "0"
                else f"email_body_{batch.id}_{source['id'].replace('.', '_')}.txt"
            )
            path = case_dir / name
            path.write_text(source["body"], encoding="utf-8")
            written_paths.append(path)
            part_id = source["id"] + ".body"
            # A pure "WG:" is the forwarded letter itself: its sender and date
            # are the original's, not the forwarder's headers.
            forward = source.get("inline_forward") or {}
            body_sender = forward.get("sender") or source_sender
            body_date = forward.get("sent_at") or source["received_date"]
            doc = Document(
                title=source_subject,
                owner_id=owner_id,
                file_path=to_storage_path(path),
                original_filename=name,
                email_source_id=source["id"],
                email_part_id=part_id,
                content_hash=hashlib.sha256(source["body"].encode()).hexdigest(),
                case_id=source_case or "_TRIAGE",
                proceeding_id=source_proceeding,
                ingest_batch_id=batch.id,
                internal_id=extract_internal_id_from_subject(source_subject) or None,
                sender=body_sender or None,
                received_date=received_date,
                issued_date=body_date,
                # The mail headers are the source of both; the body-text
                # extractors would replace them with a guess.
                extraction_confidence={
                    field: EMAIL_HEADER_CONFIDENCE
                    for field, value in (
                        ("sender", body_sender),
                        ("issued_date", body_date),
                    )
                    if value
                },
                meta=(
                    {
                        "inline_forward": {
                            "forwarder": source_sender,
                            "forwarded_at": source["received_date"].isoformat()
                            if source["received_date"]
                            else None,
                        }
                    }
                    if forward
                    else None
                ),
                page_count=0,
            )
            db.add(doc)
            db.flush()
            _pipeline_init(doc, batched=True, db=db)
            docs_to_process.append(doc)
            source["parts"].append(
                {
                    "id": part_id,
                    "filename": name,
                    "mime_type": "text/plain",
                    "outcome": "ingested",
                    "size": len(source["body"].encode()),
                    "document_ids": [doc.id],
                }
            )
        for att in source["attachments"]:
            content = att["content"]
            if not content:
                att.update(
                    outcome="empty", reason="Attachment contains no readable bytes"
                )
                continue
            if len(content) > MAX_FILE_SIZE:
                att.update(outcome="unsupported", reason="File is too large")
                continue
            magic_ext = magic_extension(content[:12])
            if (
                Path(att["filename"]).suffix.lower() not in ALLOWED_EXTENSIONS
                and magic_ext == ".pdf"
            ):
                # A PDF whose name lost its extension is still a PDF.
                att["filename"] += ".pdf"
            ext = Path(att["filename"]).suffix.lower()
            if ext not in ALLOWED_EXTENSIONS:
                att.update(outcome="unsupported", reason="File format is not supported")
                continue
            if (
                magic_ext
                and not same_format(magic_ext, ext)
                and not (magic_ext == ".zip" and ext in {".docx", ".pptx", ".xlsx"})
                # "From " is also how plain text can open.
                and not (magic_ext == ".eml" and ext in {".txt", ".md"})
            ):
                att.update(
                    outcome="unsupported",
                    reason=f"File content does not match its extension ({magic_ext})",
                )
                continue
            if ext in ARCHIVE_EXTENSIONS:
                try:
                    check_zip(content)
                except ArchiveRejected as exc:
                    att.update(outcome="unsupported", reason=str(exc))
                    continue
            att_hash = hashlib.sha256(content).hexdigest()
            existing_doc = (
                db.query(Document)
                .filter(
                    Document.content_hash == att_hash,
                    Document.owner_id == owner_id,
                    # The mail's own case, or still-unfiled copies. The same
                    # Anlage can belong to two matters: a copy filed under
                    # another case is not this mail's duplicate.
                    Document.case_id.in_({source_case, "_TRIAGE"} - {None}),
                )
                .order_by(Document.id)
                .first()
            )
            if existing_doc:
                att.update(outcome="duplicate", document_ids=[existing_doc.id])
                continue
            safe_name = _sanitize_filename(att["filename"])
            att_path = case_dir / f"{batch.id}_{safe_name}"
            stem, suffix = att_path.stem, att_path.suffix
            n = 2
            while att_path.exists():
                att_path = case_dir / f"{stem}_{n}{suffix}"
                n += 1
            att_path.write_bytes(content)
            written_paths.append(att_path)
            try:
                pdf_doc = pdfium.PdfDocument(str(att_path))
                page_count = len(pdf_doc)
                pdf_doc.close()
            except Exception:
                page_count = 0
            doc = Document(
                title=att["filename"],
                owner_id=owner_id,
                file_path=to_storage_path(att_path),
                original_filename=att["original_filename"] or att["filename"],
                email_source_id=source["id"],
                email_part_id=att["id"],
                content_hash=att_hash,
                case_id=source_case or "_TRIAGE",
                proceeding_id=source_proceeding,
                ingest_batch_id=batch.id,
                internal_id=extract_internal_id_from_subject(source_subject) or None,
                received_date=received_date,
                page_count=page_count,
            )
            db.add(doc)
            db.flush()
            att.update(outcome="ingested", document_ids=[doc.id])
            _pipeline_init(doc, batched=True, db=db)
            docs_to_process.append(doc)

    batch.source_review_reasons = bind_manifests(parsed["sources"])
    _apply_manifest_receipt(db, parsed["sources"])
    _apply_exhibits(db, parsed["sources"])
    if batch.received_source != "gmail":
        batch.source_review_reasons = sorted(
            set(batch.source_review_reasons + ["mailbox_arrival_unavailable"])
        )
    batch.email_sources = persist_sources(parsed["sources"])
    # Fully accounted duplicate-only messages remain completed tombstones.
    # Problems survive as OPEN source-review bundles even without documents.
    unresolved = [
        r for r in batch.source_review_reasons if r != "mailbox_arrival_unavailable"
    ]
    if docs_to_process or unresolved:
        batch.status = IngestBatchStatus.OPEN
    else:
        batch.status = IngestBatchStatus.CONFIRMED
        batch.meta = {**(batch.meta or {}), "reason": NO_NEW_DOCUMENTS}
    db.commit()


def _apply_manifest_receipt(db: Session, sources: list[dict]) -> None:
    """The court's delivery timestamp is the legal receipt time of its attachment.

    Only a current, validly dated entry counts. An entry bound to one document
    dates that document; one naming a delivered zip dates the documents of the
    files inside it. The batch keeps the mailbox arrival, so ``received_at``
    stays Gmail's.
    """
    for source in sources:
        for entry in source.get("attachment_manifest", []):
            if (
                entry.get("issue")
                or entry.get("historical")
                or not entry.get("timestamp")
                or not entry.get("part_id")
            ):
                continue
            if entry.get("doc_id") is not None:
                doc_ids = [entry["doc_id"]]
            else:
                prefix = entry["part_id"] + ".z"
                doc_ids = [
                    doc_id
                    for part in source.get("parts", [])
                    if part["id"].startswith(prefix)
                    for doc_id in part.get("document_ids", [])
                ]
            for doc_id in doc_ids:
                doc = db.get(Document, doc_id)
                if doc is None:
                    continue
                doc.received_date = datetime.fromisoformat(entry["timestamp"])
                doc.extraction_confidence = {
                    **(doc.extraction_confidence or {}),
                    "received_date": BEA_MANIFEST_CONFIDENCE,
                }


def _apply_exhibits(db: Session, sources: list[dict]) -> None:
    """Record each attachment's exhibit label and listed position on its document."""
    for source in sources:
        attachments = [
            (part["id"], part["filename"])
            for part in source.get("parts", [])
            if part["outcome"] == "ingested"
            and not part["id"].endswith(".body")
            and len(part.get("document_ids", [])) == 1
        ]
        if not attachments:
            continue
        documents = {
            part["id"]: part["document_ids"][0]
            for part in source.get("parts", [])
            if part.get("document_ids")
        }
        found = assign_exhibits(attachments, source.get("anlage_list") or [])
        for part_id, exhibit in found.items():
            doc = db.get(Document, documents[part_id])
            if doc is not None:
                doc.meta = {**(doc.meta or {}), "exhibit": exhibit}


def ingest_scanned_file(
    db: Session,
    pdf_path: Path,
    batch_id: str,
    source_hash: str,
    owner_id: int | None = None,
    display_name: str | None = None,
) -> IngestBatch | None:
    """Ingest a single scanned PDF from the scan folder (or an upload).

    ``display_name`` overrides the file's own name for the batch subject and a
    single-page document title (uploads are stored as ``original.pdf``).

    Returns None when the file is a duplicate (already ingested).
    Returns the created IngestBatch otherwise.
    """
    owner_id = _resolve_owner_id(db, owner_id)
    batch_repo = IngestBatchRepository(db)
    name = (display_name or pdf_path.name)[:255]

    existing = batch_repo.get_by_source_hash(source_hash, owner_id)
    if existing:
        logger.info("Scan duplicate: hash already in batch #%d — skipping", existing.id)
        return None

    batch = batch_repo.create_batch(
        source_type=IngestBatchSourceType.SCAN,
        owner_id=owner_id,
        subject=name,
        raw_source_path=to_storage_path(pdf_path),
    )
    batch.source_hash = source_hash
    try:
        db.flush()
    except IntegrityError:
        # Same race as ingest_raw_email: a concurrent scan-loop run for the
        # same file could pass the SELECT-based check above before either
        # commits. Losing the race just means the file was already ingested.
        db.rollback()
        logger.info(
            "Scan duplicate (race): hash already ingested by a concurrent run — skipping"
        )
        return None

    logger.info("Scan batch #%d created: file=%s", batch.id, pdf_path.name)

    try:
        pdf_doc = pdfium.PdfDocument(str(pdf_path))
        page_count = len(pdf_doc)
        pdf_doc.close()
    except Exception as exc:
        db.rollback()
        raise ValueError(f"Cannot open PDF: {exc}") from exc

    if page_count == 1:
        # Single-page: create Document directly and dispatch
        # source_hash is already the SHA-256 of this file's bytes (the scan driver
        # computed it), so don't read the whole PDF into memory a second time.
        content_hash = source_hash
        doc = Document(
            title=name,
            owner_id=owner_id,
            file_path=to_storage_path(pdf_path),
            original_filename=name,
            content_hash=content_hash,
            case_id="_TRIAGE",
            ingest_batch_id=batch.id,
            page_count=page_count,
        )
        from app.services.pipeline_status import initialize as _pipeline_init

        db.add(doc)
        db.flush()
        _pipeline_init(doc, batched=False, db=db)
        batch.status = IngestBatchStatus.OPEN
        db.commit()
        logger.info("Scan batch #%d: single-page PDF, dispatching extraction", batch.id)
        dispatch_task("app.tasks.document_processing.process_document_task", doc.id)
    else:
        # Multi-page: queue slicing; no Documents yet
        batch.meta = {
            "slicing": {
                "status": "preparing",
                "page_count": page_count,
                # Lets recover_stuck_slicing_prep tell "still running" from "lost".
                "dispatched_at": now_utc().isoformat(),
            }
        }
        batch.status = IngestBatchStatus.AWAITING_SLICING
        db.commit()
        logger.info("Scan batch #%d: %d pages → queuing slicing", batch.id, page_count)
        dispatch_task("app.tasks.prepare_slicing.prepare_slicing_task", batch.id)

    return batch
