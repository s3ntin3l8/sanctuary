"""Unit tests for extract_with_chandra's concurrency wiring.

Focus: the OCR-concurrency changes — `max_workers` no longer a hardcoded
constant but a caller-supplied value, and each page acquires the global
`ocr_slots.ocr_slot()` semaphore around its HTTP call (nested inside the
existing per-document `model_gate("chandra")` hold). The HTTP call itself,
Redis, and PDF rendering are all mocked — this is not an integration test.
"""

import contextlib
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


def _patch_common(page_count: int):
    """Patch rendering + HTTP + both gates; return (render_mock, ocr_page_mock, slot_mock)."""
    pngs = [f"page-{i}".encode() for i in range(page_count)]
    render = patch(
        "app.services.ingestion.chandra_extractor._render_pdf_to_pngs",
        return_value=pngs,
    )
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
            "app.services.ingestion.chandra_extractor.ThreadPoolExecutor"
        ) as pool_cls,
    ):
        pool_cls.return_value.__enter__.return_value.map.return_value = iter(
            [(1, "<p>a</p>", "a", 0.1, None), (2, "<p>b</p>", "b", 0.1, None)]
        )
        extract_with_chandra("doc.pdf", ocr_config=_OCR_CFG, max_workers=2)
    pool_cls.assert_called_once_with(max_workers=2)


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
            "app.services.ingestion.chandra_extractor.ThreadPoolExecutor"
        ) as pool_cls,
    ):
        pool_cls.return_value.__enter__.return_value.map.return_value = iter(
            [(1, "<p>a</p>", "a", 0.1, None)]
        )
        extract_with_chandra("doc.pdf", ocr_config=_OCR_CFG, max_workers=4)
    pool_cls.assert_called_once_with(max_workers=1)


@pytest.mark.unit
def test_model_gate_held_once_for_whole_document_not_per_page():
    render, ocr_page, gate, slot, slot_mock = _patch_common(page_count=5)
    with render, ocr_page, gate as gate_mock, slot:
        extract_with_chandra("doc.pdf", ocr_config=_OCR_CFG, max_workers=8)
    gate_mock.assert_called_once_with("chandra", label="chandra-extract:doc.pdf")
