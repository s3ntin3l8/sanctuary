"""PDF → markdown extraction via Chandra-OCR-2 (or any OpenAI-compat OCR model).

Production module — talks HTTP to whichever endpoint the user picked as the
active OCR instance. Returns the same ``{content, metadata, chunks}`` dict
shape as ``app.services.ingestion.converters.convert_file`` so it drops into
the existing ingest pipeline as a swap-in alternative to Docling+Tesseract.

Architecture choices, learned during the benchmark in `benchmarks/vision_vs_markdown/`:

- One image per HTTP request (matches upstream chandra/model/vllm.py;
  vLLM batches concurrent requests on the loaded model anyway).
- Pages rendered with pypdfium2 at 192 DPI (chandra's IMAGE_DPI default).
- Page-parallel via ThreadPoolExecutor — multi-page docs complete in
  ``ceil(pages / max_workers)`` round-trips instead of N.
- ``reasoning_content`` fallback for Qwen-based OCR models, which often
  emit schema-constrained output through the reasoning channel.
- HTML→markdown via markdownify (chandra emits HTML with layout-block
  ``<div data-bbox=...>`` wrappers which we strip for readability).
"""

from __future__ import annotations

import base64
import io
import logging
import time
from concurrent.futures import ThreadPoolExecutor
from typing import Any

import httpx
import pypdfium2 as pdfium
from markdownify import markdownify

from app.config import AI_READ_TIMEOUT
from app.services.ai_config import OcrConfig
from app.services.model_gate import model_gate

logger = logging.getLogger(__name__)

# Chandra's tuned defaults — see chandra/settings.py + chandra/scripts/vllm.py
# upstream. These match the values the model was trained/tuned against;
# changing them tends to degrade extraction quality.
CHANDRA_DPI = 192
CHANDRA_MAX_OUTPUT_TOKENS = 12384
CHANDRA_TEMPERATURE = 0.0
CHANDRA_TOP_P = 0.1

# Page-parallel concurrency cap. vLLM batches concurrent requests, so this
# is the per-document parallelism we ask for. 8 matches the benchmark
# config which was stable against the 24GB single-GPU LMStudio setup.
DEFAULT_PAGE_WORKERS = 8


# Verbatim from chandra/prompts.py — the model is trained on this exact text,
# so paraphrasing or trimming any of it degrades extraction quality.
_ALLOWED_TAGS = [
    "math",
    "br",
    "i",
    "b",
    "u",
    "del",
    "sup",
    "sub",
    "table",
    "tr",
    "td",
    "p",
    "th",
    "div",
    "pre",
    "h1",
    "h2",
    "h3",
    "h4",
    "h5",
    "ul",
    "ol",
    "li",
    "input",
    "a",
    "span",
    "img",
    "hr",
    "tbody",
    "small",
    "caption",
    "strong",
    "thead",
    "big",
    "code",
    "chem",
]
_ALLOWED_ATTRIBUTES = [
    "class",
    "colspan",
    "rowspan",
    "display",
    "checked",
    "type",
    "border",
    "value",
    "style",
    "href",
    "alt",
    "align",
    "data-bbox",
    "data-label",
]
_PROMPT_ENDING = f"""
Only use these tags {_ALLOWED_TAGS}, and these attributes {_ALLOWED_ATTRIBUTES}.

Guidelines:
* Inline math: Surround math with <math>...</math> tags. Math expressions should be rendered in KaTeX-compatible LaTeX. Use display for block math.
* Tables: Use colspan and rowspan attributes to match table structure.
* Formatting: Maintain consistent formatting with the image, including spacing, indentation, subscripts/superscripts, and special characters.
* Images: Include a description of any images in the alt attribute of an <img> tag. Do not fill out the src property. Describe in detail inside the div tag. Also convert charts to high fidelity data, and convert diagrams to mermaid.
* Forms: Mark checkboxes and radio buttons properly.
* Text: join lines together properly into paragraphs using <p>...</p> tags.  Use <br> tags for line breaks within paragraphs, but only when absolutely necessary to maintain meaning.
* Chemistry: Use <chem>...</chem> tags for chemical formulas with reactive SMILES.
* Lists: Preserve indents and proper list markers.
* Use the simplest possible HTML structure that accurately represents the content of the block.
* Make sure the text is accurate and easy for a human to read and interpret.  Reading order should be correct and natural.
""".strip()

OCR_LAYOUT_PROMPT = f"""
OCR this image to HTML, arranged as layout blocks.  Each layout block should be a div with the data-bbox attribute representing the bounding box of the block in x0 y0 x1 y1 format.  Bboxes are normalized 0-1000. The data-label attribute is the label for the block.

Use the following labels:
- Caption
- Footnote
- Equation-Block
- List-Group
- Page-Header
- Page-Footer
- Image
- Section-Header
- Table
- Text
- Complex-Block
- Code-Block
- Form
- Table-Of-Contents
- Figure
- Chemical-Block
- Diagram
- Bibliography
- Blank-Page

{_PROMPT_ENDING}
""".strip()


class ChandraExtractionError(RuntimeError):
    """Raised when chandra extraction fails irrecoverably (no usable text)."""


def _render_pdf_to_pngs(file_path: str, *, dpi: int = CHANDRA_DPI) -> list[bytes]:
    """Render every page of a PDF to PNG bytes with pypdfium2.

    Matches the rendering convention used by `app/services/ingestion/converters.py`
    (scale = dpi / 72, bitmap.to_pil → PNG). All pages — no max-pages cap;
    extraction has to see the whole document or we'd silently lose content.
    """
    pdf = pdfium.PdfDocument(file_path)
    try:
        scale = dpi / 72
        out: list[bytes] = []
        for i in range(len(pdf)):
            bitmap = pdf[i].render(scale=scale)
            img = bitmap.to_pil()
            buf = io.BytesIO()
            img.save(buf, format="PNG", optimize=True)
            out.append(buf.getvalue())
        return out
    finally:
        pdf.close()


def _ocr_one_page(
    png_bytes: bytes,
    *,
    url: str,
    headers: dict,
    model: str,
    timeout: float,
) -> str:
    """POST a single page image to chandra, return raw HTML response.

    Raises on transport/HTTP failure so the caller can record the failed page
    and continue with the rest.
    """
    b64 = base64.b64encode(png_bytes).decode("ascii")
    payload = {
        "model": model,
        "messages": [
            {
                "role": "user",
                "content": [
                    {
                        "type": "image_url",
                        "image_url": {"url": f"data:image/png;base64,{b64}"},
                    },
                    {"type": "text", "text": OCR_LAYOUT_PROMPT},
                ],
            }
        ],
        "stream": False,
        "temperature": CHANDRA_TEMPERATURE,
        "top_p": CHANDRA_TOP_P,
        "max_tokens": CHANDRA_MAX_OUTPUT_TOKENS,
    }
    with httpx.Client(timeout=httpx.Timeout(timeout)) as client:
        resp = client.post(url, json=payload, headers=headers)
        resp.raise_for_status()
        body = resp.json()
    msg = body["choices"][0]["message"]
    # Chandra is built on Qwen3.5 base, which often emits schema-constrained
    # output through reasoning_content rather than content even with thinking
    # disabled. Production _ai_call.py applies the same fallback.
    return (msg.get("content") or "").strip() or (
        msg.get("reasoning_content") or ""
    ).strip()


def _html_to_markdown(html: str) -> str:
    """Lossy but pragmatic HTML→markdown for the chandra layout-block output.

    Strips the wrapping ``<div data-bbox=... data-label=...>`` blocks but
    keeps their inner content (paragraphs, headings, tables, image alt text).
    Collapses runs of >2 blank lines for readability in the HUD.
    """
    if not html.strip():
        return ""
    md = markdownify(html, heading_style="ATX", strip=["div"])
    lines: list[str] = []
    blank = 0
    for line in md.splitlines():
        if not line.strip():
            blank += 1
            if blank <= 2:
                lines.append(line)
        else:
            blank = 0
            lines.append(line)
    return "\n".join(lines).strip()


def extract_with_chandra(
    file_path: str,
    *,
    ocr_config: OcrConfig,
    dpi: int = CHANDRA_DPI,
    max_workers: int = DEFAULT_PAGE_WORKERS,
    timeout: float | None = None,
) -> dict[str, Any]:
    """Extract a PDF using the configured Chandra OCR endpoint.

    Returns the same dict shape ``convert_file`` does:
        ``{"content": str, "metadata": dict, "chunks": list}``

    Per-page concurrency is bounded by ``max_workers``. ``metadata`` carries
    ``pages``, ``extractor: "chandra-ocr-2"``, ``page_failures`` (1-indexed
    page numbers that failed OCR), and per-page extraction latency.

    Raises ``ChandraExtractionError`` if ``ocr_config.ocr_model`` is empty
    (no model configured) or every page fails.
    """
    if not ocr_config.ocr_model:
        raise ChandraExtractionError(
            "No OCR model configured on the active OCR instance — "
            "set one in Settings → AI & Models, or switch the extraction "
            "engine back to Docling."
        )

    base_url = ocr_config.base_url.rstrip("/")
    api_key = ocr_config.api_key
    timeout = timeout or AI_READ_TIMEOUT
    url = f"{base_url}/v1/chat/completions"
    headers = {"Content-Type": "application/json"}
    if api_key and api_key != "not-needed":
        headers["Authorization"] = f"Bearer {api_key}"

    start = time.perf_counter()
    page_pngs = _render_pdf_to_pngs(file_path, dpi=dpi)
    if not page_pngs:
        raise ChandraExtractionError(f"PDF has no renderable pages: {file_path}")

    def _ocr_safe(
        item: tuple[int, bytes],
    ) -> tuple[int, str, str, float, Exception | None]:
        idx, png = item
        page_started = time.perf_counter()
        try:
            html = _ocr_one_page(
                png,
                url=url,
                headers=headers,
                model=ocr_config.ocr_model,
                timeout=timeout,
            )
            md = _html_to_markdown(html)
            return idx, html, md, time.perf_counter() - page_started, None
        except Exception as exc:  # noqa: BLE001 — per-page resilience
            logger.warning(
                "chandra OCR failed on page %d of %s: %s", idx, file_path, exc
            )
            return (
                idx,
                "",
                f"<!-- chandra page {idx} failed: {exc} -->",
                time.perf_counter() - page_started,
                exc,
            )

    workers = max(1, min(max_workers, len(page_pngs)))
    results: list[tuple[int, str, str, float, Exception | None]] = []
    # Hold the chandra family lock for the whole document so the per-page
    # ThreadPoolExecutor below shares one gate acquisition. This prevents
    # cross-family thrashing with qwen when the ai workers are also active
    # — same-family OCR calls coalesce naturally on the loaded model.
    with model_gate("chandra", label=f"chandra-extract:{file_path}"):
        with ThreadPoolExecutor(max_workers=workers) as pool:
            # executor.map preserves input order — needed for page ordering.
            for r in pool.map(_ocr_safe, enumerate(page_pngs, start=1)):
                results.append(r)

    page_failures = [idx for idx, _, _, _, exc in results if exc is not None]
    if len(page_failures) == len(results):
        raise ChandraExtractionError(
            f"All {len(results)} pages failed OCR for {file_path}"
        )

    content = "\n\n".join(
        f"--- PAGE {idx} ---\n\n{md}" for idx, _, md, _, _ in results
    ).strip()

    chunks = [
        {
            "text": md,
            "meta": {
                "page": idx,
                "source": "chandra-ocr-2",
                "ocr_model": ocr_config.ocr_model,
                "latency_seconds": round(elapsed, 2),
                "failed": exc is not None,
            },
        }
        for idx, _, md, elapsed, exc in results
    ]

    metadata = {
        "pages": len(results),
        "format": "pdf",
        "extractor": "chandra-ocr-2",
        "ocr_model": ocr_config.ocr_model,
        "ocr_base_url": base_url,
        "extraction_seconds": round(time.perf_counter() - start, 2),
        "page_failures": page_failures,
        "extraction_engine": "chandra",
    }

    logger.info(
        "chandra extracted %s: %d pages, %d failures, %d chars, %.1fs",
        file_path,
        len(results),
        len(page_failures),
        len(content),
        metadata["extraction_seconds"],
    )

    return {"content": content, "metadata": metadata, "chunks": chunks}
