import concurrent.futures
import logging
import multiprocessing
import os
import threading

from app.config import INGEST_CONVERSION_TIMEOUT

logger = logging.getLogger(__name__)

CONVERSION_TIMEOUT = INGEST_CONVERSION_TIMEOUT  # seconds


class TimeoutError(Exception):
    pass


_allowed_extensions = {".pdf", ".docx", ".txt", ".md", ".pptx", ".xlsx", ".eml"}
MAX_FILE_SIZE = 50 * 1024 * 1024  # 50MB

MAGIC_BYTES = {
    b"%PDF": ".pdf",
    b"PK\x03\x04": ".zip",
    b"PK\x05\x06": ".zip",
    b"\xd0\xcf\x11\xe0": ".doc",
    b"From ": ".eml",
}

# Known Docling glyph mapping artifacts (Common in German legal PDFs)
_GLYPH_MAP = {
    "GLYPH(cmap:df00)": "G",
}


def _apply_glyph_fixes(text: str | None) -> str | None:
    """Fix common Docling extraction artifacts like GLYPH(cmap:df00)."""
    if not text:
        return text
    for glyph, char in _GLYPH_MAP.items():
        text = text.replace(glyph, char)
    return text


def validate_file_magic(file_path: str) -> str | None:
    """Validate file by magic bytes. Returns expected extension or None."""
    try:
        with open(file_path, "rb") as f:
            header = f.read(8)
        for magic, ext in MAGIC_BYTES.items():
            if header.startswith(magic):
                return ext
        return None
    except OSError:
        return None


def is_allowed_extension(filename: str) -> bool:
    """Check if file extension is allowed."""
    ext = os.path.splitext(filename)[1].lower()
    return ext in _allowed_extensions


ALLOWED_EXTENSIONS = _allowed_extensions


def get_allowed_extensions() -> set:
    """Get allowed file extensions."""
    return _allowed_extensions


def parse_eml_file(file_path: str) -> str:
    """Parse .eml file to extract text content."""
    from email import policy
    from email.parser import BytesParser

    with open(file_path, "rb") as f:
        msg = BytesParser(policy=policy.default).parse(f)

    content = []
    subject = msg.get("Subject", "")
    if subject:
        content.append(f"Subject: {subject}")

    from_addr = msg.get("From", "")
    if from_addr:
        content.append(f"From: {from_addr}")

    date = msg.get("Date", "")
    if date:
        content.append(f"Date: {date}")

    to_addr = msg.get("To", "")
    if to_addr:
        content.append(f"To: {to_addr}")

    if msg.is_multipart():
        for part in msg.walk():
            content_type = part.get_content_type()
            if content_type == "text/plain":
                try:
                    payload = part.get_payload(decode=True)
                    if payload:
                        content.append(payload.decode("utf-8", errors="ignore"))
                except Exception as e:
                    logger.debug(f"Failed to decode email part: {e}")
    else:
        try:
            payload = msg.get_payload(decode=True)
            if payload:
                content.append(payload.decode("utf-8", errors="ignore"))
        except Exception as e:
            logger.debug(f"Failed to decode email payload: {e}")

    return "\n\n".join(content)


_converter: object | None = None
_converter_lock = threading.Lock()


def _get_converter():
    """Lazy-init the Docling DocumentConverter on first use (thread-safe)."""
    global _converter
    if _converter is None:
        with _converter_lock:
            if _converter is None:
                import time

                start = time.perf_counter()
                logger.info("Initializing Docling DocumentConverter...")
                try:
                    from docling.datamodel.base_models import InputFormat
                    from docling.datamodel.pipeline_options import (
                        PdfPipelineOptions,
                        TableFormerMode,
                        TesseractCliOcrOptions,
                    )
                    from docling.document_converter import (
                        DocumentConverter,
                        PdfFormatOption,
                    )

                    pipeline_options = PdfPipelineOptions()
                    pipeline_options.do_table_structure = True
                    pipeline_options.table_structure_options.mode = (
                        TableFormerMode.ACCURATE
                    )
                    pipeline_options.do_ocr = True
                    pipeline_options.ocr_options = TesseractCliOcrOptions(
                        lang=["deu", "eng"]
                    )

                    _converter = DocumentConverter(
                        format_options={
                            InputFormat.PDF: PdfFormatOption(
                                pipeline_options=pipeline_options
                            )
                        }
                    )
                    elapsed = time.perf_counter() - start
                    logger.info(
                        "Docling DocumentConverter initialized in %.2fs", elapsed
                    )
                except Exception as e:
                    raise RuntimeError(
                        f"Failed to initialize Docling converter: {e}. "
                        "Ensure Docling is installed and system dependencies are met."
                    ) from e
    return _converter


def _convert_in_subprocess(file_path: str) -> dict:
    """Run the full Docling conversion + chunking in a worker subprocess.

    Each subprocess lazy-initializes its own DocumentConverter on first call
    and reuses it for the rest of its lifetime. Defined at module scope so it
    pickles cleanly into the worker.
    """
    ext = os.path.splitext(file_path)[1].lower()
    conv = _get_converter()
    result = conv.convert(file_path)

    metadata = {
        "pages": len(result.document.pages) if hasattr(result.document, "pages") else 1,
        "format": result.input.format.value if hasattr(result.input, "format") else ext,
    }

    from docling.chunking import HierarchicalChunker

    chunker = HierarchicalChunker()
    chunks = []
    try:
        doc_chunks = list(chunker.chunk(result.document))
        for chunk in doc_chunks:
            chunks.append(
                {
                    "text": chunk.text,
                    "meta": {
                        "doc_items": [
                            str(item.self_ref) for item in chunk.meta.doc_items
                        ]
                        if hasattr(chunk.meta, "doc_items")
                        else [],
                        "headings": chunk.meta.headings
                        if hasattr(chunk.meta, "headings")
                        else [],
                    },
                }
            )
    except Exception as e:
        logger.warning(f"Chunking failed for {file_path}: {e}")

    markdown = result.document.export_to_markdown()
    markdown = _apply_glyph_fixes(markdown)
    for chunk in chunks:
        chunk["text"] = _apply_glyph_fixes(chunk["text"])

    return {"content": markdown, "metadata": metadata, "chunks": chunks}


_conversion_pool: concurrent.futures.ProcessPoolExecutor | None = None
_pool_lock = threading.Lock()


def _get_pool() -> concurrent.futures.ProcessPoolExecutor:
    """Lazy-init the single-worker subprocess pool used for conversions.

    `max_workers=1` serializes conversions; `spawn` keeps the worker isolated
    from parent imports (so SQLAlchemy / Celery state doesn't leak in)."""
    global _conversion_pool
    if _conversion_pool is None:
        with _pool_lock:
            if _conversion_pool is None:
                ctx = multiprocessing.get_context("spawn")
                _conversion_pool = concurrent.futures.ProcessPoolExecutor(
                    max_workers=1, mp_context=ctx
                )
    return _conversion_pool


def _reset_pool() -> None:
    """Tear down the worker pool — kills any runaway Docling subprocess from a
    timeout so the next conversion starts fresh instead of inheriting the hang."""
    global _conversion_pool
    with _pool_lock:
        if _conversion_pool is not None:
            _conversion_pool.shutdown(wait=False, cancel_futures=True)
            _conversion_pool = None


def convert_file(file_path: str, timeout: int = None) -> dict:
    """Convert file to markdown using Docling and extract structural metadata.

    Conversion runs in a single-worker subprocess pool so that a hang inside
    Docling/Tesseract OCR can be terminated by recycling the worker, instead
    of leaking a zombie daemon thread that holds memory until the parent exits.
    """
    if timeout is None:
        timeout = CONVERSION_TIMEOUT

    ext = os.path.splitext(file_path)[1].lower()

    if ext == ".eml":
        return {
            "content": parse_eml_file(file_path),
            "metadata": {"pages": 1},
            "chunks": [],
        }

    pool = _get_pool()
    future = pool.submit(_convert_in_subprocess, file_path)
    try:
        return future.result(timeout=timeout)
    except concurrent.futures.TimeoutError:
        _reset_pool()
        raise TimeoutError(f"Conversion timed out after {timeout} seconds") from None


def is_valid_docling_output(content: str | None) -> bool:
    """Check if Docling produced valid output."""
    if not content:
        return False
    stripped = content.strip()
    if not stripped:
        return False
    if stripped.startswith("Conversion failed:"):
        return False
    return not len(stripped) < 5
