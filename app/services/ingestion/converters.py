import concurrent.futures
import html
import logging
import os
import re
import threading

from docling.datamodel.pipeline_options import DOCLING_LAYOUT_EGRET_LARGE

from app.config import INGEST_CONVERSION_TIMEOUT

logger = logging.getLogger(__name__)

CONVERSION_TIMEOUT = INGEST_CONVERSION_TIMEOUT  # seconds

# Layout model used by Docling's PDF/IMAGE pipelines. EGRET_LARGE outperforms
# the default HERON on German legal-document layouts in our cross-document tests
# (footer leak detection, heading consistency, reading order, table-vs-picture
# classification). EGRET_XLARGE regressed on the same metrics, so LARGE is the
# right size for our document type.
_LAYOUT_MODEL_SPEC = DOCLING_LAYOUT_EGRET_LARGE


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

    # 1. Unescape HTML entities Docling injects into plain-text exports
    #    (e.g. "Pietsch &amp; Hönig" → "Pietsch & Hönig"). Apply before the
    #    <unknown> strip so the escaped variant also gets caught.
    text = html.unescape(text)

    # 2. Strip <unknown> placeholders. Docling emits these where the layout
    #    model could read text but couldn't classify it (often around stamp
    #    watermarks, e.g. "Beglaubigte Abschrift" on certified copies).
    #    The placeholder itself carries no information; dropping is safer
    #    than leaving the literal "<unknown>" string in the markdown.
    text = text.replace("<unknown>", "")

    # 3. Strip phantom auto-numbered prefixes on enumerated legal sub-items.
    #    Docling sometimes prepends "4." to a "2.b.a) …" sub-item because it
    #    sees an outer ordered list. The leading number is never legitimate
    #    in front of legal sub-numbering (e.g. "2.b.a)", "2.b.b)").
    text = re.sub(
        r"(?m)^\d+\.\s+(\d+\.[a-z](?:\.[a-z])?\)\s)",
        r"\1",
        text,
    )

    # 4. Strip leading single-character noise paragraphs at the document head.
    #    Some forms (Jugendamt Hilfeplan etc.) produce "paragraphs" that are
    #    just one stray punctuation char or letter (": / u / ' / | / |") before
    #    the real content starts. Trim until the first non-trivial paragraph.
    text = _strip_leading_noise_paragraphs(text)

    return text


# Pure punctuation or a single bare letter. Legitimate short section markers
# ("I.", "II.", "1.", "a)") all combine alphanumerics with punctuation across
# at least two chars and are preserved.
_NOISE_PARAGRAPH_RE = re.compile(r"^[\W_]{1,3}$|^[A-Za-zÄÖÜäöüß]$")


def _strip_leading_noise_paragraphs(text: str) -> str:
    """Drop one- or two-char punctuation/single-letter "paragraphs" at the head.

    Stops at the first paragraph whose stripped length is > 5 — beyond that,
    short paragraphs deep in the document are kept (they may be legitimate
    section markers or numbered items).
    """
    paragraphs = text.split("\n\n")
    head_end = 0
    for i, para in enumerate(paragraphs):
        stripped = para.strip()
        if len(stripped) > 5:
            head_end = i
            break
        if _NOISE_PARAGRAPH_RE.match(stripped):
            continue
        # A short paragraph that isn't pure noise — keep it and stop trimming.
        head_end = i
        break
    else:
        # Whole document is short/noise — leave it alone to avoid eating
        # everything on a near-empty page.
        return text
    return "\n\n".join(paragraphs[head_end:])


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
            LayoutOptions,
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
        # EGRET_LARGE outperforms the default HERON on German legal-document
        # layouts (footer detection, heading consistency, reading order,
        # table-vs-picture classification). See .claude/plans/why-is-docling-not-
        # squishy-waterfall.md for the cross-document comparison.
        pipeline_options.layout_options = LayoutOptions(model_spec=_LAYOUT_MODEL_SPEC)
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
        LayoutOptions,
        PdfPipelineOptions,
        TableFormerMode,
        TesseractCliOcrOptions,
    )
    from docling.document_converter import DocumentConverter, ImageFormatOption

    pipeline_options = PdfPipelineOptions()
    pipeline_options.do_table_structure = True
    pipeline_options.table_structure_options.mode = TableFormerMode.ACCURATE
    pipeline_options.do_ocr = True
    pipeline_options.layout_options = LayoutOptions(model_spec=_LAYOUT_MODEL_SPEC)
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


_IMAGE_PLACEHOLDER = "<!-- image -->"
_IMAGE_PLACEHOLDER_RE = re.compile(re.escape(_IMAGE_PLACEHOLDER))


def _collect_pictures(document) -> dict[int, list]:
    """Group docling PictureItem objects by 1-indexed page number, in reading order.

    Docling's `pictures` list is already in document reading order; we just bucket
    by page. Pictures without provenance are skipped.
    """
    by_page: dict[int, list] = {}
    pics = getattr(document, "pictures", None) or []
    for pic in pics:
        prov = getattr(pic, "prov", None)
        if not prov:
            continue
        page_no = getattr(prov[0], "page_no", None)
        if not page_no:
            continue
        by_page.setdefault(page_no, []).append(pic)
    return by_page


def _pdf_text_in_bbox(textpage, bbox) -> str:
    """Read text from a docling BoundingBox via pypdfium2.

    Docling bbox is bottom-left origin in PDF points (l/t/r/b where t > b).
    pypdfium2 get_text_bounded uses the same convention: (left, bottom, right, top).
    """
    try:
        text = textpage.get_text_bounded(bbox.l, bbox.b, bbox.r, bbox.t)
    except Exception as e:
        logger.debug("get_text_bounded failed: %s", e)
        return ""
    return (text or "").strip()


def _ocr_picture_region(page, bbox) -> str:
    """OCR the rendered picture region via the image-input docling pipeline.

    Used when the PDF text layer has nothing under the picture (true image-only
    regions like signature scans or stamped seals with image-only glyphs).
    """
    import tempfile
    from pathlib import Path

    try:
        page_w, page_h = page.get_size()
        bitmap = page.render(scale=300 / 72)
        img = bitmap.to_pil()
        img_w, img_h = img.size
        sx, sy = img_w / page_w, img_h / page_h
        # Convert bottom-left PDF coords -> top-left pixel coords for PIL crop
        x0 = max(0, int(bbox.l * sx))
        y0 = max(0, int((page_h - bbox.t) * sy))
        x1 = min(img_w, int(bbox.r * sx))
        y1 = min(img_h, int((page_h - bbox.b) * sy))
        if x1 - x0 < 10 or y1 - y0 < 10:
            return ""
        cropped = img.crop((x0, y0, x1, y1))

        with tempfile.TemporaryDirectory() as tmpdir:
            img_path = str(Path(tmpdir) / "pic.png")
            cropped.save(img_path)
            md, _, _ = _run_conversion(_get_image_ocr_converter(), img_path)
        cleaned = _PLACEHOLDER_RE.sub("", md).strip()
        # Reject results that are just noise or another image placeholder
        if len(re.sub(r"\s+", "", cleaned)) < 3:
            return ""
        return cleaned
    except Exception as e:
        logger.debug("OCR of picture region failed: %s", e)
        return ""


def _recover_picture_text(
    file_path: str, pictures_by_page: dict[int, list]
) -> dict[int, str]:
    """For each picture, recover text from the PDF text layer or via region OCR.

    Returns a mapping from id(picture) -> recovered text (empty string if neither
    source produced anything usable).
    """
    if not pictures_by_page:
        return {}

    import pypdfium2 as pdfium

    out: dict[int, str] = {}
    try:
        pdf = pdfium.PdfDocument(file_path)
    except Exception as e:
        logger.debug("Failed to open PDF for picture recovery: %s", e)
        return {id(p): "" for pics in pictures_by_page.values() for p in pics}

    try:
        for page_no, pics in pictures_by_page.items():
            try:
                page = pdf[page_no - 1]
            except Exception:
                for p in pics:
                    out[id(p)] = ""
                continue
            textpage = None
            try:
                textpage = page.get_textpage()
                for pic in pics:
                    bbox = pic.prov[0].bbox
                    text = _pdf_text_in_bbox(textpage, bbox)
                    if not text:
                        text = _ocr_picture_region(page, bbox)
                    out[id(pic)] = text
            finally:
                if textpage is not None:
                    textpage.close()
                page.close()
    finally:
        pdf.close()
    return out


def _substitute_picture_placeholders(page_md: str, picture_texts: list[str]) -> str:
    """Replace each `<!-- image -->` placeholder in reading order with recovered text.

    Empty entries keep the placeholder in place. If the placeholder count doesn't
    match the picture count for this page (shouldn't normally happen — docling
    emits one placeholder per picture in reading order), append the non-empty
    recovered texts at the end of the page as a safety net so no content is lost.
    """
    placeholder_count = page_md.count(_IMAGE_PLACEHOLDER)
    if placeholder_count == 0 or not picture_texts:
        return page_md

    if placeholder_count != len(picture_texts):
        logger.warning(
            "Picture placeholder count mismatch (md=%d, pictures=%d) — appending recovered text at page tail",
            placeholder_count,
            len(picture_texts),
        )
        extras = [t for t in picture_texts if t]
        if not extras:
            return page_md
        return page_md.rstrip() + "\n\n" + "\n\n".join(extras)

    iterator = iter(picture_texts)

    def _replace(_match):
        text = next(iterator)
        if not text:
            return _IMAGE_PLACEHOLDER
        return f"\n\n{text}\n\n"

    return _IMAGE_PLACEHOLDER_RE.sub(_replace, page_md)


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

    pictures_by_page = _collect_pictures(result.document)

    # Export with page break placeholders so we can inject explicit headers
    page_break = "<!-- PAGE_BREAK -->"
    markdown = result.document.export_to_markdown(page_break_placeholder=page_break)
    parts = markdown.split(page_break)

    recovered = 0
    # Skip picture recovery when the standard pass produced essentially nothing
    # but image placeholders — that's the sandwich-PDF signal, and the
    # _convert_in_subprocess fallback handles those by reading the full PDF
    # text layer (which gives cleaner full-document text than piecemeal
    # picture-region recovery would).
    if ext == ".pdf" and pictures_by_page and not _is_image_only_output(markdown):
        picture_texts = _recover_picture_text(file_path, pictures_by_page)
        recovered = sum(1 for v in picture_texts.values() if v)
        if recovered:
            new_parts = []
            for i, part in enumerate(parts):
                pics = pictures_by_page.get(i + 1, [])
                if pics and _IMAGE_PLACEHOLDER in part:
                    texts = [picture_texts.get(id(p), "") for p in pics]
                    part = _substitute_picture_placeholders(part, texts)
                new_parts.append(part)
            parts = new_parts
            metadata["picture_text_recovered"] = recovered

    # Post-process to add --- PAGE {N} --- markers. Docling doesn't support
    # dynamic placeholders like {page_no} yet.
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


def convert_file(
    file_path: str, timeout: int = None, *, engine: str = "docling"
) -> dict:
    """Convert file to markdown and extract structural metadata.

    ``engine`` controls PDF extraction: ``"chandra"`` routes through the
    Chandra-OCR vision pipeline (active OCR instance from settings); anything
    else uses the existing Docling+Tesseract subprocess. Non-PDF formats
    always use the existing path — Chandra is image-based and adds nothing
    for text-native formats. If Chandra extraction raises (no OCR model
    configured, endpoint unreachable, all pages failed) we fall back to
    Docling so a misconfigured OCR endpoint never bricks ingestion.
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

    if ext in {".txt", ".md"}:
        with open(file_path, encoding="utf-8", errors="ignore") as f:
            content = f.read()
        return {"content": content, "metadata": {"pages": 1}, "chunks": []}

    if ext == ".pdf" and engine == "chandra":
        try:
            from app.config import SessionLocal
            from app.services.ai_config import get_ocr_config
            from app.services.ingestion.chandra_extractor import (
                ChandraExtractionError,
                extract_with_chandra,
            )

            session = SessionLocal()
            try:
                ocr_cfg = get_ocr_config(session)
            finally:
                session.close()
            return extract_with_chandra(file_path, ocr_config=ocr_cfg)
        except ChandraExtractionError as exc:
            logger.warning(
                "Chandra extraction failed for %s — falling back to Docling: %s",
                file_path,
                exc,
            )
        except Exception as exc:  # noqa: BLE001 — never let OCR brick ingest
            logger.warning(
                "Chandra extraction crashed for %s — falling back to Docling: %s",
                file_path,
                exc,
            )

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
