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
import threading
import time
from collections.abc import Generator
from concurrent.futures import ALL_COMPLETED, Future, ThreadPoolExecutor, wait
from typing import Any

import httpx
import pypdfium2 as pdfium
from markdownify import markdownify

from app.config import AI_READ_TIMEOUT, CHANDRA_DOCUMENT_DEADLINE_SECONDS
from app.services.ai_config import OcrConfig
from app.services.ingestion import ocr_crosscheck
from app.services.model_gate import model_gate
from app.services.ocr_slots import ocr_slot

logger = logging.getLogger(__name__)

# Chandra's tuned defaults — see chandra/settings.py + chandra/scripts/vllm.py
# upstream. These match the values the model was trained/tuned against;
# changing them tends to degrade extraction quality.
CHANDRA_DPI = 192
CHANDRA_MAX_OUTPUT_TOKENS = 12384
CHANDRA_TEMPERATURE = 0.0
CHANDRA_TOP_P = 0.1

# Page-parallel concurrency cap, used only when the caller doesn't resolve
# the "OCR Concurrency" Settings value (see get_ocr_concurrency in
# app.services.user_settings_service). Kept in sync with that setting's
# default by convention. The real global cap across documents is
# app.services.ocr_slots — this just bounds a single document's own thread
# pool so a lone large scan doesn't spin up more threads than there are
# slots to fill.
DEFAULT_PAGE_WORKERS = 4


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


def _page_count(file_path: str) -> int:
    # Opened separately from _render_pages on purpose: reopening a PDF is cheap
    # next to threading the count out of the generator's handle, and it lets the
    # caller fail fast on an empty document before taking the model gate.
    pdf = pdfium.PdfDocument(file_path)
    try:
        return len(pdf)
    finally:
        pdf.close()


def _render_pages(
    file_path: str, *, dpi: int = CHANDRA_DPI
) -> Generator[bytes, None, None]:
    """Lazily render a PDF's pages, in order, to PNG bytes with pypdfium2.

    Matches the rendering convention used by `app/services/ingestion/converters.py`
    (scale = dpi / 72, bitmap.to_pil -> PNG). All pages -- no max-pages cap;
    extraction has to see the whole document or we'd silently lose content.

    A generator on purpose: at 192 DPI one page's PNG is hundreds of KB, so
    rendering a whole scan up front held every page in memory for the whole OCR
    run. Consumers pull one page at a time and the page/bitmap/image handles are
    closed as soon as its PNG is built. pypdfium2 is not thread-safe, so this
    must be drained from a single thread.
    """
    pdf = pdfium.PdfDocument(file_path)
    try:
        scale = dpi / 72
        for i in range(len(pdf)):
            page = pdf[i]
            bitmap = page.render(scale=scale)
            img = bitmap.to_pil()
            try:
                buf = io.BytesIO()
                img.save(buf, format="PNG", optimize=True)
                png = buf.getvalue()
            finally:
                img.close()
                bitmap.close()
                page.close()
            yield png
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
    document_deadline: float | None = None,
) -> dict[str, Any]:
    """Extract a PDF using the configured Chandra OCR endpoint.

    Returns the same dict shape ``convert_file`` does:
        ``{"content": str, "metadata": dict, "chunks": list}``

    Per-page concurrency is bounded by ``max_workers``. ``metadata`` carries
    ``pages``, ``extractor: "chandra-ocr-2"``, ``page_failures`` (1-indexed
    page numbers that failed OCR), ``ocr_unverified_pages`` (pages whose text a
    second OCR could not corroborate, see ``ocr_crosscheck``), and per-page
    extraction latency.

    ``document_deadline`` bounds the *whole document's* wall-clock time
    (default ``CHANDRA_DOCUMENT_DEADLINE_SECONDS``), independent of each
    page's own ``timeout``. Each page call is individually bounded by its own
    httpx client timeout already, so no single page can hang forever — but a
    many-page document worked through a handful of page-parallel workers has
    no overall cap without this, and could otherwise run for as long as
    ``ceil(pages / max_workers) * timeout``. Pages still pending once the
    deadline passes are recorded as failed (not awaited further) so the
    document returns with whatever pages did complete rather than losing all
    of that OCR work to a later Celery-level hard kill.

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
    document_deadline = (
        document_deadline
        if document_deadline is not None
        else CHANDRA_DOCUMENT_DEADLINE_SECONDS
    )
    url = f"{base_url}/v1/chat/completions"
    headers = {"Content-Type": "application/json"}
    if api_key and api_key != "not-needed":
        headers["Authorization"] = f"Bearer {api_key}"

    page_count = _page_count(file_path)
    if not page_count:
        raise ChandraExtractionError(f"PDF has no renderable pages: {file_path}")

    # Set once the document deadline has passed. _ocr_safe checks this before
    # (and after) acquiring the global ocr_slot so a page whose thread hadn't
    # started its HTTP call yet bails out immediately instead of acquiring
    # model_gate/ocr_slot-adjacent resources on behalf of a result that will
    # be discarded — the outer model_gate("chandra") hold ends as soon as we
    # give up waiting, so any OCR call still made after that point runs
    # without the cross-family protection that gate exists for. A page
    # already mid-HTTP-call when the deadline fires can't be interrupted;
    # it stays bounded by its own per-page httpx timeout regardless.
    abandoned = threading.Event()
    # Page -> an independent second reading of the same image, to catch pages
    # chandra answered fluently but wrongly (see ocr_crosscheck).
    second_readings: dict[int, str] = {}

    def _ocr_safe(
        item: tuple[int, bytes],
    ) -> tuple[int, str, str, float, Exception | None]:
        idx, png = item
        page_started = time.perf_counter()
        if abandoned.is_set():
            return (
                idx,
                "",
                f"<!-- chandra page {idx} skipped: document deadline exceeded -->",
                0.0,
                TimeoutError("document deadline exceeded"),
            )
        try:
            # Global cross-document slot, held only for the network call —
            # this is what lets N single-page documents (each with a
            # thread pool of 1) still saturate N slots together, and lets
            # a mixed batch (e.g. 1-page + 8-page) split the remaining
            # slots instead of the big doc hogging up to 8 on its own.
            with ocr_slot(label=f"{file_path}:page:{idx}"):
                if abandoned.is_set():
                    return (
                        idx,
                        "",
                        f"<!-- chandra page {idx} skipped: "
                        "document deadline exceeded -->",
                        time.perf_counter() - page_started,
                        TimeoutError("document deadline exceeded"),
                    )
                html = _ocr_one_page(
                    png,
                    url=url,
                    headers=headers,
                    model=ocr_config.ocr_model,
                    timeout=timeout,
                )
            md = _html_to_markdown(html)
            if (
                not abandoned.is_set()
                and (second := ocr_crosscheck.second_opinion(png)) is not None
            ):
                second_readings[idx] = second
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

    workers = max(1, min(max_workers, page_count))
    results: list[tuple[int, str, str, float, Exception | None]] = []
    # Hold the chandra family lock for the whole document so the per-page
    # ThreadPoolExecutor below shares one gate acquisition. This prevents
    # cross-family thrashing with qwen when the ai workers are also active
    # — same-family OCR calls (from this doc AND any other doc extracting
    # concurrently) coalesce naturally on the loaded model. The actual
    # cross-document concurrency cap is the per-page ocr_slot() semaphore
    # inside _ocr_safe above, not this gate — chandra holders don't
    # exclude each other here.
    with model_gate("chandra", label=f"chandra-extract:{file_path}"):
        # document_deadline counts from here, not from when this call started
        # waiting for the gate -- a long qwen-contention wait would otherwise
        # consume the whole OCR budget before a single page is even
        # attempted, marking every page failed and silently falling back to
        # Docling on every gate-contended document.
        start = time.perf_counter()
        pool = ThreadPoolExecutor(max_workers=workers)
        pages = _render_pages(file_path, dpi=dpi)
        try:
            # At most `workers` rendered pages exist at once (queued for a thread,
            # waiting on the global ocr_slot, or mid-call): a permit is taken
            # before a page is rendered and returned when its OCR future ends.
            in_flight = threading.BoundedSemaphore(workers)
            future_pages: dict[Future, int] = {}
            unsubmitted: list[int] = []
            for idx in range(1, page_count + 1):
                remaining = max(0.0, document_deadline - (time.perf_counter() - start))
                if not in_flight.acquire(timeout=remaining):
                    unsubmitted = list(range(idx, page_count + 1))
                    break
                try:
                    png = next(pages)
                except BaseException:
                    in_flight.release()
                    raise
                fut = pool.submit(_ocr_safe, (idx, png))
                fut.add_done_callback(lambda _f: in_flight.release())
                future_pages[fut] = idx
                del png  # the future's argument is now the only reference

            remaining = max(0.0, document_deadline - (time.perf_counter() - start))
            done, not_done = wait(
                future_pages, timeout=remaining, return_when=ALL_COMPLETED
            )
            for fut in done:
                results.append(fut.result())
            if not_done or unsubmitted:
                logger.warning(
                    "chandra document deadline (%ss) exceeded for %s — "
                    "%d/%d page(s) still in flight or never started, "
                    "returning partial result",
                    document_deadline,
                    file_path,
                    len(not_done) + len(unsubmitted),
                    page_count,
                )
                incomplete = [future_pages[fut] for fut in not_done] + unsubmitted
                for idx in incomplete:
                    results.append(
                        (
                            idx,
                            "",
                            f"<!-- chandra page {idx} incomplete: "
                            "document deadline exceeded -->",
                            time.perf_counter() - start,
                            TimeoutError("document deadline exceeded"),
                        )
                    )
        finally:
            # Set unconditionally (not just on a deadline exceeded) so any
            # page thread that hasn't yet reached its ocr_slot()/HTTP-call
            # checkpoints bails immediately rather than acquiring a slot (or
            # making an OCR call outside model_gate's protection) on behalf
            # of a result we're discarding — including if wait() itself
            # raised (e.g. a soft time limit), which would otherwise skip
            # straight past the `if not_done` branch above without ever
            # setting it.
            abandoned.set()
            pages.close()  # release the PDF handle even if rendering stopped early
            # Not a plain `with` block: on a deadline exceeded, we must not
            # block here waiting for the still-running pages either — they
            # stay bounded by their own per-page httpx timeout and simply
            # finish in the background. abandoned.set() above is what makes a
            # not-yet-started page thread short-circuit; cancel_futures is only
            # belt and braces for a future that is still queued (the bounded
            # in-flight window means there should be none).
            pool.shutdown(wait=False, cancel_futures=True)

    results.sort(key=lambda r: r[0])  # restore page order (submit+wait doesn't)
    page_failures = [idx for idx, _, _, _, exc in results if exc is not None]
    if len(page_failures) == len(results):
        raise ChandraExtractionError(
            f"All {len(results)} pages failed OCR for {file_path}"
        )

    content = "\n\n".join(
        f"--- PAGE {idx} ---\n\n{md}" for idx, _, md, _, _ in results
    ).strip()

    checks = {
        idx: ocr_crosscheck.check_page(md, second_readings[idx])
        for idx, _, md, _, exc in results
        if exc is None and idx in second_readings
    }

    chunks = [
        {
            "text": md,
            "meta": {
                "page": idx,
                "source": "chandra-ocr-2",
                "ocr_model": ocr_config.ocr_model,
                "latency_seconds": round(elapsed, 2),
                "failed": exc is not None,
                **({"crosscheck": checks[idx]} if idx in checks else {}),
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
        "ocr_unverified_pages": [
            idx for idx, check in checks.items() if ocr_crosscheck.is_unverified(check)
        ],
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
