"""Per-page OCR text of an extracted PDF, and its reuse when the PDF is split.

The Chandra extractor keeps one chunk per page (``meta.chunks[].meta.page``).
Splitting a document into parts would otherwise OCR the same pages again, so a
part is handed the source's reading of its own pages.

The Docling engine keeps no per-page chunks, but writes ``--- PAGE N ---``
markers into the content; those answer ``page_texts`` (and so the packet hint
and hold) when they are complete.
"""

from __future__ import annotations

import re
from typing import Any

from app.models.database import Document

# Metadata of the source that still describes a part cut from its pages.
_INHERITED_KEYS = (
    "format",
    "extractor",
    "ocr_model",
    "ocr_base_url",
    "extraction_engine",
    "ocr_crosscheck_unavailable",
)


def _usable_chunks(doc: Document) -> dict[int, dict[str, Any]]:
    """The source's page chunks that were read successfully, by 1-based page."""
    usable: dict[int, dict[str, Any]] = {}
    for chunk in (doc.meta or {}).get("chunks") or []:
        meta = chunk.get("meta") or {}
        page = meta.get("page")
        if isinstance(page, int) and not meta.get("failed") and chunk.get("text"):
            usable[page] = chunk
    return usable


_PAGE_MARK = re.compile(r"(?m)^--- PAGE (\d+) ---$")
_PLACEHOLDER = re.compile(r"<!--.*?-->", re.DOTALL)


def _marked_pages(doc: Document) -> dict[int, str]:
    """Page texts cut from the ``--- PAGE N ---`` markers of the content.

    Only when the markers are exactly 1..``meta.pages``: a PDF whose pages the
    markers do not account for is not read page by page, and a wrong page split
    would hold or hint wrongly. Pages that are empty or only an image
    placeholder are left out.
    """
    pages = (doc.meta or {}).get("pages")
    if not doc.content or not isinstance(pages, int):
        return {}
    parts = _PAGE_MARK.split(doc.content)
    numbers = [int(n) for n in parts[1::2]]
    if numbers != list(range(1, pages + 1)):
        return {}
    texts = {}
    for number, text in zip(numbers, parts[2::2], strict=True):
        text = _PLACEHOLDER.sub("", text).strip()
        if text:
            texts[number] = text
    return texts


def page_texts(doc: Document) -> dict[int, str]:
    """1-based page -> text, for the pages that were read successfully."""
    chunked = any(
        isinstance((chunk.get("meta") or {}).get("page"), int)
        for chunk in (doc.meta or {}).get("chunks") or []
    )
    if chunked:
        return {page: chunk["text"] for page, chunk in _usable_chunks(doc).items()}
    return _marked_pages(doc)


def extraction_for_pages(doc: Document, pages: list[int]) -> dict[str, Any] | None:
    """The extraction result of a part made of ``pages`` (1-based, in order).

    Same shape ``convert_file`` returns. ``None`` when any of the pages has no
    usable reading, so the part is extracted from scratch instead.
    """
    chunks = _usable_chunks(doc)
    if not pages or any(p not in chunks for p in pages):
        return None

    source_meta = doc.meta or {}
    unverified = set(source_meta.get("ocr_unverified_pages") or [])
    part_chunks = []
    for number, page in enumerate(pages, start=1):
        chunk = chunks[page]
        part_chunks.append(
            {"text": chunk["text"], "meta": {**chunk["meta"], "page": number}}
        )
    content = "\n\n".join(
        f"--- PAGE {c['meta']['page']} ---\n\n{c['text']}" for c in part_chunks
    ).strip()
    metadata: dict[str, Any] = {
        **{k: source_meta[k] for k in _INHERITED_KEYS if k in source_meta},
        "pages": len(pages),
        "reused_from_document": doc.id,
        "page_failures": [],
        "ocr_unverified_pages": [
            number for number, page in enumerate(pages, start=1) if page in unverified
        ],
    }
    return {"content": content, "metadata": metadata, "chunks": part_chunks}
