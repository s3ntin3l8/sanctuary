import concurrent.futures
import logging
import os
import re
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
    "Äug": "Aug.",
    "Äug.": "Aug.",
    "esizese": "festgesetzt",
    "Verfah'erswwe7": "Verfahrenswert",
    "Bescserdeverfanren": "Beschwerdeverfahren",
}


def _apply_glyph_fixes(text: str | None) -> str | None:
    """Fix common Docling extraction artifacts like GLYPH(cmap:df00)."""
    if not text:
        return text
    for glyph, char in _GLYPH_MAP.items():
        text = text.replace(glyph, char)

    # Bavarian court footer artifacts (commonly seen in OLG München / AG Ingolstadt docs)
    # "Datenschutzhinweis" often breaks due to non-standard font mapping in the footer.
    text = re.sub(r"D\s*hutzhi\s*[_:]", "Datenschutzhinweis:", text)
    text = re.sub(r"Dat\s*hutzhi\s*12[;:]", "Datenschutzhinweis:", text)

    # Standardize court-style date spacing artifacts (e.g., "0 1. Aug" -> "01. Aug")
    text = re.sub(r"(\d)\s+(\d\.)\s+([A-Z][a-z]{2})", r"\1\2 \3", text)

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
_ocr_converter: object | None = None
_ocr_converter_lock = threading.Lock()


def _build_converter(force_full_page_ocr: bool = False):
    """Construct a Docling DocumentConverter with Tesseract OCR configured."""
    import time

    label = "force_full_page_ocr" if force_full_page_ocr else "standard"
    start = time.perf_counter()
    logger.info("Initializing Docling DocumentConverter (%s)...", label)
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
        pipeline_options.table_structure_options.mode = TableFormerMode.ACCURATE
        pipeline_options.do_ocr = True
        pipeline_options.ocr_options = TesseractCliOcrOptions(
            lang=["deu", "eng"],
            force_full_page_ocr=force_full_page_ocr,
        )
        if force_full_page_ocr:
            # Render at 2× scale so Tesseract gets higher-resolution input on
            # scanned pages where the layout model produced only image placeholders.
            pipeline_options.images_scale = 2.0

        conv = DocumentConverter(
            format_options={
                InputFormat.PDF: PdfFormatOption(pipeline_options=pipeline_options)
            }
        )
        logger.info(
            "Docling DocumentConverter (%s) initialized in %.2fs",
            label,
            time.perf_counter() - start,
        )
        return conv
    except Exception as e:
        raise RuntimeError(
            f"Failed to initialize Docling converter: {e}. "
            "Ensure Docling is installed and system dependencies are met."
        ) from e


def _get_converter():
    """Lazy-init the standard DocumentConverter (thread-safe)."""
    global _converter
    if _converter is None:
        with _converter_lock:
            if _converter is None:
                _converter = _build_converter(force_full_page_ocr=False)
    return _converter


def _get_ocr_converter():
    """Lazy-init the force_full_page_ocr DocumentConverter for scanned PDFs (thread-safe)."""
    global _ocr_converter
    if _ocr_converter is None:
        with _ocr_converter_lock:
            if _ocr_converter is None:
                _ocr_converter = _build_converter(force_full_page_ocr=True)
    return _ocr_converter


_image_ocr_converter = None
_image_ocr_converter_lock = threading.Lock()


def _build_image_ocr_converter():
    """Construct a Docling DocumentConverter for IMAGE input with Tesseract OCR.

    Used by the rotation-corrected OCR path, which renders pages via pypdfium2
    at 300 DPI with corrected orientation and passes the PNGs into Docling's
    IMAGE pipeline. Pre-rendered at 300 DPI so images_scale stays at 1.0.
    """
    from docling.datamodel.base_models import InputFormat
    from docling.datamodel.pipeline_options import (
        PdfPipelineOptions,
        TableFormerMode,
        TesseractCliOcrOptions,
    )
    from docling.document_converter import DocumentConverter, ImageFormatOption

    pipeline_options = PdfPipelineOptions()
    pipeline_options.do_table_structure = True
    pipeline_options.table_structure_options.mode = TableFormerMode.ACCURATE
    pipeline_options.do_ocr = True
    pipeline_options.ocr_options = TesseractCliOcrOptions(
        lang=["deu", "eng"],
        force_full_page_ocr=True,
    )
    pipeline_options.images_scale = 1.0  # pre-rendered at 300 DPI, no extra scaling

    return DocumentConverter(
        format_options={
            InputFormat.IMAGE: ImageFormatOption(pipeline_options=pipeline_options)
        }
    )


def _get_image_ocr_converter():
    """Lazy-init the IMAGE-input DocumentConverter (thread-safe)."""
    global _image_ocr_converter
    if _image_ocr_converter is None:
        with _image_ocr_converter_lock:
            if _image_ocr_converter is None:
                _image_ocr_converter = _build_image_ocr_converter()
    return _image_ocr_converter


def _is_image_only_output(content: str | None) -> bool:
    """Return True when docling output is exclusively image placeholders.

    Distinguishes scanned PDFs (layout model saw images, no text) from PDFs
    with actual embedded text. Only these need a force_full_page_ocr retry.
    """
    if not content:
        return False
    stripped = content.strip()
    # has_placeholders should only check for actual image markers (<!-- image -->)
    # and not our injected page markers (--- PAGE N ---).
    has_placeholders = bool(re.search(r"<!--[^>]*-->", stripped))
    cleaned = _PLACEHOLDER_RE.sub("", stripped)
    word_chars = len(re.sub(r"\s+", "", cleaned))
    return has_placeholders and word_chars < 30


def _run_conversion(conv, file_path: str) -> tuple[str, list, dict]:
    """Run one Docling conversion pass; return (markdown, chunks, metadata)."""
    ext = os.path.splitext(file_path)[1].lower()
    result = conv.convert(file_path)

    metadata = {
        "pages": len(result.document.pages) if hasattr(result.document, "pages") else 1,
        "format": result.input.format.value if hasattr(result.input, "format") else ext,
    }

    from docling.chunking import HierarchicalChunker

    chunks = []
    try:
        for chunk in HierarchicalChunker().chunk(result.document):
            chunks.append(
                {
                    "text": _apply_glyph_fixes(chunk.text),
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
        logger.warning("Chunking failed for %s: %s", file_path, e)

    # Export with page break placeholders so we can inject explicit headers
    page_break = "<!-- PAGE_BREAK -->"
    markdown = result.document.export_to_markdown(page_break_placeholder=page_break)

    # Post-process to add --- PAGE {N} --- markers. Docling doesn't support
    # dynamic placeholders like {page_no} yet.
    parts = markdown.split(page_break)
    processed_parts = []
    for i, part in enumerate(parts):
        header = f"--- PAGE {i + 1} ---"
        processed_parts.append(f"{header}\n\n{part.strip()}")

    markdown = _apply_glyph_fixes("\n\n".join(processed_parts))
    return markdown, chunks, metadata


def _extract_pdf_text_layer(file_path: str) -> str | None:
    """Extract the embedded text layer from a sandwich PDF using pypdfium2.

    Returns formatted markdown with --- PAGE N --- markers, or None if the
    text layer is absent or contains fewer than 100 non-whitespace characters.
    Sandwich PDFs (scanner output) have text rendered invisible (Tr=3) so the
    layout model ignores it — this reads it directly from the PDF structure.
    """
    try:
        import pypdfium2 as pdfium

        doc = pdfium.PdfDocument(file_path)
        parts = []
        for i, page in enumerate(doc):
            textpage = page.get_textpage()
            text = textpage.get_text_range().strip()
            textpage.close()
            page.close()
            if text:
                parts.append(f"--- PAGE {i + 1} ---\n\n{text}")
        doc.close()
        combined = "\n\n".join(parts)
        if len(re.sub(r"\s+", "", combined)) < 100:
            return None
        return _apply_glyph_fixes(combined)
    except Exception as e:
        logger.debug("pdf text layer extraction failed for %s: %s", file_path, e)
        return None


def _ocr_with_rotation_correction(file_path: str) -> tuple[str, list, dict]:
    """Per-page rotation-corrected OCR for truly scanned PDFs (no text layer).

    pypdfium2 does not automatically apply a PDF page's /Rotate attribute when
    rendering; if the scanner stored pages as rotated (e.g. 270°), Tesseract
    sees a sideways image and misses content. This function:

    1. Reads each page's stored rotation via page.get_rotation().
    2. Renders at the inverse angle at 300 DPI to produce an upright image.
    3. Runs the Docling IMAGE pipeline (with table detection) on each page.
    4. Combines per-page markdown with --- PAGE N --- markers.

    If the metadata-corrected render still yields image-only output for a page
    (rotation metadata absent/wrong), that page is retried at +90° as a safety
    net before giving up on that page.
    """
    import tempfile
    from pathlib import Path

    import pypdfium2 as pdfium

    RENDER_SCALE = 300 / 72  # 300 DPI

    pdf = pdfium.PdfDocument(file_path)
    page_parts = []
    any_rotation_applied = False

    with tempfile.TemporaryDirectory() as tmpdir:
        conv = _get_image_ocr_converter()
        for i, page in enumerate(pdf):
            stored_rot = page.get_rotation()
            correction = (360 - stored_rot) % 360
            if correction:
                any_rotation_applied = True

            bitmap = page.render(scale=RENDER_SCALE, rotation=correction)
            img = bitmap.to_pil()
            page.close()

            img_path = str(Path(tmpdir) / f"page_{i + 1}.png")
            img.save(img_path)

            page_md, _, _ = _run_conversion(conv, img_path)

            # Safety net: metadata rotation absent or wrong — try +90°
            if _is_image_only_output(page_md):
                img_90_path = str(Path(tmpdir) / f"page_{i + 1}_90.png")
                img.rotate(-90, expand=True).save(img_90_path)
                page_md_90, _, _ = _run_conversion(conv, img_90_path)
                if not _is_image_only_output(page_md_90):
                    page_md = page_md_90
                    any_rotation_applied = True

            # Strip any PAGE markers Docling added (each image = 1-page doc)
            cleaned = _PLACEHOLDER_RE.sub("", page_md).strip()
            page_parts.append(f"--- PAGE {i + 1} ---\n\n{cleaned}")

    pdf.close()

    metadata = {
        "pages": len(page_parts),
        "format": "pdf",
        "ocr_fallback": True,
    }
    if any_rotation_applied:
        metadata["ocr_rotation_corrected"] = True

    combined = _apply_glyph_fixes("\n\n".join(page_parts))
    return combined, [], metadata


def _convert_in_subprocess(file_path: str) -> dict:
    """Run the full Docling conversion + chunking in a worker subprocess.

    Each subprocess lazy-initializes its own DocumentConverter on first call
    and reuses it for the rest of its lifetime. Defined at module scope so it
    pickles cleanly into the worker.

    When the standard pass produces only image placeholders (scanned PDF whose
    pages the layout model classified as pictures rather than text), we first
    try to extract the PDF's own embedded text layer (sandwich PDFs from
    scanners carry an invisible OCR layer that is more complete than re-running
    Tesseract on a JPEG). If no usable text layer exists, fall back to
    rotation-corrected per-page OCR via the IMAGE pipeline.
    """
    markdown, chunks, metadata = _run_conversion(_get_converter(), file_path)

    if _is_image_only_output(markdown):
        text_layer = _extract_pdf_text_layer(file_path)
        if text_layer:
            logger.info(
                "Sandwich PDF detected — using embedded text layer: %s",
                file_path,
            )
            markdown = text_layer
            metadata["pdf_text_layer"] = True
        else:
            logger.info(
                "Scanned PDF detected (no text layer) — running "
                "rotation-corrected OCR: %s",
                file_path,
            )
            markdown, chunks, metadata = _ocr_with_rotation_correction(file_path)

    return {"content": markdown, "metadata": metadata, "chunks": chunks}


_conversion_executor: concurrent.futures.ThreadPoolExecutor | None = None
_executor_lock = threading.Lock()


def _get_executor() -> concurrent.futures.ThreadPoolExecutor:
    """Lazy-init a single-worker thread pool for serialising conversions."""
    global _conversion_executor
    if _conversion_executor is None:
        with _executor_lock:
            if _conversion_executor is None:
                _conversion_executor = concurrent.futures.ThreadPoolExecutor(
                    max_workers=1, thread_name_prefix="docling"
                )
    return _conversion_executor


def convert_file(file_path: str, timeout: int = None) -> dict:
    """Convert file to markdown using Docling and extract structural metadata."""
    if timeout is None:
        timeout = CONVERSION_TIMEOUT

    ext = os.path.splitext(file_path)[1].lower()

    if ext == ".eml":
        return {
            "content": parse_eml_file(file_path),
            "metadata": {"pages": 1},
            "chunks": [],
        }

    if ext in {".txt", ".md"}:
        with open(file_path, encoding="utf-8", errors="ignore") as f:
            content = f.read()
        return {"content": content, "metadata": {"pages": 1}, "chunks": []}

    executor = _get_executor()
    future = executor.submit(_convert_in_subprocess, file_path)
    try:
        return future.result(timeout=timeout)
    except concurrent.futures.TimeoutError:
        raise TimeoutError(f"Conversion timed out after {timeout} seconds") from None


_PLACEHOLDER_RE = re.compile(r"<!--[^>]*-->|--- PAGE \d+ ---")


def is_valid_docling_output(content: str | None) -> bool:
    """Check if Docling produced usable text output (not just image placeholders)."""
    if not content:
        return False
    stripped = content.strip()
    if not stripped or len(stripped) < 5:
        return False
    if stripped.startswith("Conversion failed:"):
        return False
    # Reject content that is mostly image/page-break placeholders with no real text
    cleaned = _PLACEHOLDER_RE.sub("", stripped)
    word_chars = len(re.sub(r"\s+", "", cleaned))
    return word_chars >= 30
