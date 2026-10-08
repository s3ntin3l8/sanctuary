"""Unit tests for extract_with_chandra's concurrency wiring.

Focus: the OCR-concurrency changes — `max_workers` no longer a hardcoded
constant but a caller-supplied value, and each page acquires the global
`ocr_slots.ocr_slot()` semaphore around its HTTP call (nested inside the
existing per-document `model_gate("chandra")` hold). The HTTP call itself,
Redis, and PDF rendering are all mocked — this is not an integration test.
"""

import contextlib
from concurrent.futures import ThreadPoolExecutor
from unittest.mock import MagicMock, patch

import pytest

from app.services.ai_config import OcrConfig
from app.services.ingestion.chandra_extractor import (
    DEFAULT_PAGE_WORKERS,
    extract_with_chandra,
)

_OCR_CFG = OcrConfig(
    id="ocr-1",
    label="test",
    base_url="http://ocr.local",
    provider="openai",
    api_key="not-needed",  # pragma: allowlist secret
    ocr_model="chandra-ocr-2",
)


@contextlib.contextmanager
def _fake_gate(*_args, **_kwargs):
    yield "sentinel"


def _patch_render(page_count: int, rendered: list | None = None):
    """Patch page counting + lazy rendering for a ``page_count``-page document."""
    pngs = [f"page-{i}".encode() for i in range(page_count)]

    def _gen(*_a, **_k):
        for png in pngs:
            if rendered is not None:
                rendered.append(png)
            yield png

    stack = contextlib.ExitStack()
    stack.enter_context(
        patch(
            "app.services.ingestion.chandra_extractor._page_count",
            return_value=page_count,
        )
    )
    stack.enter_context(
        patch(
            "app.services.ingestion.chandra_extractor._render_pages",
            side_effect=_gen,
        )
    )
    return stack


def _patch_common(page_count: int):
    """Patch rendering + HTTP + both gates; return (render_mock, ocr_page_mock, slot_mock)."""
    render = _patch_render(page_count)
    ocr_page = patch(
        "app.services.ingestion.chandra_extractor._ocr_one_page",
        return_value="<p>hi</p>",
    )
    gate = patch(
        "app.services.ingestion.chandra_extractor.model_gate", side_effect=_fake_gate
    )
    slot_mock = MagicMock(side_effect=_fake_gate)
    slot = patch("app.services.ingestion.chandra_extractor.ocr_slot", slot_mock)
    return render, ocr_page, gate, slot, slot_mock


@pytest.mark.unit
def test_default_max_workers_is_four():
    """DEFAULT_PAGE_WORKERS now matches the OCR-concurrency setting's default."""
    assert DEFAULT_PAGE_WORKERS == 4


@pytest.mark.unit
def test_ocr_slot_acquired_once_per_page():
    render, ocr_page, gate, slot, slot_mock = _patch_common(page_count=3)
    with render, ocr_page, gate, slot:
        result = extract_with_chandra("doc.pdf", ocr_config=_OCR_CFG, max_workers=8)
    assert slot_mock.call_count == 3
    assert result["metadata"]["pages"] == 3
    assert result["metadata"]["page_failures"] == []


@pytest.mark.unit
def test_max_workers_param_overrides_default():
    """A caller-supplied max_workers (e.g. from get_ocr_concurrency) is honored,
    not silently clamped back to DEFAULT_PAGE_WORKERS."""
    render, ocr_page, gate, slot, slot_mock = _patch_common(page_count=2)
    with (
        render,
        ocr_page,
        gate,
        slot,
        patch(
            "app.services.ingestion.chandra_extractor.ThreadPoolExecutor",
            wraps=ThreadPoolExecutor,
        ) as pool_cls,
    ):
        result = extract_with_chandra("doc.pdf", ocr_config=_OCR_CFG, max_workers=2)
    pool_cls.assert_called_once_with(max_workers=2)
    assert result["metadata"]["page_failures"] == []


@pytest.mark.unit
def test_workers_capped_at_page_count_even_with_higher_setting():
    """A 1-page doc with OCR concurrency=4 still only spins up 1 thread —
    the page-level global slot semaphore (not this pool) is what lets other
    documents fill the remaining 3 slots."""
    render, ocr_page, gate, slot, slot_mock = _patch_common(page_count=1)
    with (
        render,
        ocr_page,
        gate,
        slot,
        patch(
            "app.services.ingestion.chandra_extractor.ThreadPoolExecutor",
            wraps=ThreadPoolExecutor,
        ) as pool_cls,
    ):
        extract_with_chandra("doc.pdf", ocr_config=_OCR_CFG, max_workers=4)
    pool_cls.assert_called_once_with(max_workers=1)


@pytest.mark.unit
def test_model_gate_held_once_for_whole_document_not_per_page():
    render, ocr_page, gate, slot, slot_mock = _patch_common(page_count=5)
    with render, ocr_page, gate as gate_mock, slot:
        extract_with_chandra("doc.pdf", ocr_config=_OCR_CFG, max_workers=8)
    gate_mock.assert_called_once_with("chandra", label="chandra-extract:doc.pdf")


@pytest.mark.unit
def test_document_deadline_returns_partial_result_for_pages_still_in_flight():
    """PR3b: a many-page document has no overall wall-clock cap otherwise —
    each page's own httpx timeout only bounds that one page. A document_
    deadline that elapses while some pages are still in flight must return
    the pages that did complete plus a failure marker for the rest, rather
    than blocking until every page is done -- verified here by elapsed wall
    time, not just by the result's contents (a `pool.shutdown(wait=True)`
    regression would still produce the same result, just slowly)."""
    import threading
    import time

    page_count = 3
    slow_page_block_seconds = 5.0
    release = threading.Event()
    pngs = [f"page-{i}".encode() for i in range(page_count)]
    slow_png = pngs[-1]

    def _one_slow_page(png_bytes, *, url, headers, model, timeout):
        if png_bytes == slow_png:
            # Blocks well past document_deadline below, simulating a page
            # call still in flight when the document-level budget runs out;
            # the other two pages return immediately.
            release.wait(timeout=slow_page_block_seconds)
            return "<p>late</p>"
        return "<p>fast</p>"

    with (
        _patch_render(page_count),
        patch(
            "app.services.ingestion.chandra_extractor._ocr_one_page",
            side_effect=_one_slow_page,
        ),
        patch(
            "app.services.ingestion.chandra_extractor.model_gate",
            side_effect=_fake_gate,
        ),
        patch(
            "app.services.ingestion.chandra_extractor.ocr_slot", side_effect=_fake_gate
        ),
    ):
        started = time.perf_counter()
        try:
            result = extract_with_chandra(
                "doc.pdf",
                ocr_config=_OCR_CFG,
                max_workers=page_count,
                document_deadline=1.0,
            )
        finally:
            elapsed = time.perf_counter() - started
            release.set()

    # Must return once its own document_deadline passes, not once the slow
    # page's block eventually clears -- the whole point of not doing a
    # blocking pool.shutdown(wait=True).
    assert elapsed < slow_page_block_seconds / 2
    assert result["metadata"]["pages"] == page_count
    assert result["metadata"]["page_failures"] == [page_count]
    assert "document deadline exceeded" in result["content"]
    assert result["content"].count("fast") == page_count - 1


@pytest.mark.unit
def test_only_a_bounded_window_of_pages_is_rendered_ahead_of_ocr():
    """The point of #141: a 40-page scan must not have 40 PNGs in memory. With
    2 workers, page N+2 is not rendered until an earlier page's OCR finished."""
    import threading

    rendered: list[bytes] = []
    gate_open = threading.Event()
    started = threading.Semaphore(0)

    def _blocked_page(png_bytes, *, url, headers, model, timeout):
        started.release()
        gate_open.wait(timeout=10)
        return "<p>x</p>"

    result: dict = {}

    def _run():
        with (
            _patch_render(8, rendered),
            patch(
                "app.services.ingestion.chandra_extractor._ocr_one_page",
                side_effect=_blocked_page,
            ),
            patch(
                "app.services.ingestion.chandra_extractor.model_gate",
                side_effect=_fake_gate,
            ),
            patch(
                "app.services.ingestion.chandra_extractor.ocr_slot",
                side_effect=_fake_gate,
            ),
        ):
            result["r"] = extract_with_chandra(
                "doc.pdf", ocr_config=_OCR_CFG, max_workers=2
            )

    t = threading.Thread(target=_run)
    t.start()
    assert started.acquire(timeout=10) and started.acquire(timeout=10)
    # Two pages are mid-OCR (blocked); the producer must be waiting, not racing ahead.
    import time

    time.sleep(0.5)
    assert len(rendered) == 2, f"rendered {len(rendered)} pages ahead of OCR"

    gate_open.set()
    t.join(timeout=20)
    assert len(rendered) == 8
    assert result["r"]["metadata"]["pages"] == 8
    assert result["r"]["metadata"]["page_failures"] == []
    assert [c["meta"]["page"] for c in result["r"]["chunks"]] == list(range(1, 9))


@pytest.mark.unit
def test_pages_not_started_before_the_deadline_are_reported_incomplete():
    import threading

    release = threading.Event()

    def _slow(png_bytes, *, url, headers, model, timeout):
        release.wait(timeout=5)
        return "<p>late</p>"

    try:
        with (
            _patch_render(6),
            patch(
                "app.services.ingestion.chandra_extractor._ocr_one_page",
                side_effect=_slow,
            ),
            patch(
                "app.services.ingestion.chandra_extractor.model_gate",
                side_effect=_fake_gate,
            ),
            patch(
                "app.services.ingestion.chandra_extractor.ocr_slot",
                side_effect=_fake_gate,
            ),
            pytest.raises(Exception, match="All 6 pages failed"),
        ):
            extract_with_chandra(
                "doc.pdf", ocr_config=_OCR_CFG, max_workers=2, document_deadline=0.5
            )
    finally:
        release.set()


@pytest.mark.unit
def test_render_pages_streams_real_pdf_pages_and_closes_handles(tmp_path):
    import pypdfium2 as pdfium

    from app.services.ingestion.chandra_extractor import _page_count, _render_pages

    doc = pdfium.PdfDocument.new()
    for _ in range(3):
        doc.new_page(100, 100)
    path = tmp_path / "three.pdf"
    doc.save(str(path))
    doc.close()

    assert _page_count(str(path)) == 3
    gen = _render_pages(str(path), dpi=50)
    first = next(gen)
    assert first.startswith(b"\x89PNG")
    gen.close()  # stopping early must not leak the PDF handle or raise

    assert len(list(_render_pages(str(path), dpi=50))) == 3
