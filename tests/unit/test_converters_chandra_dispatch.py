"""Unit test: convert_file's Chandra branch resolves max_workers from the
OCR-concurrency Settings value rather than leaving extract_with_chandra to
fall back to its own hardcoded DEFAULT_PAGE_WORKERS.

All the imports convert_file makes inside its Chandra branch are late
(module-body-local `from X import Y` statements executed at call time), so
they're patched at their source module, not as `converters.<name>`
attributes — converters.py itself never binds these names at import time.
"""

from unittest.mock import MagicMock, patch

import pytest

from app.services.ingestion.converters import convert_file


@pytest.mark.unit
def test_convert_file_chandra_passes_ocr_concurrency_as_max_workers():
    fake_result = {
        "content": "hello",
        "metadata": {"extractor": "chandra-ocr-2", "pages": 1},
        "chunks": [],
    }
    fake_session = MagicMock()

    with (
        patch("app.config.SessionLocal", return_value=fake_session),
        patch(
            "app.services.ai_config.get_ocr_config",
            return_value=MagicMock(ocr_model="chandra-ocr-2"),
        ),
        patch(
            "app.services.user_settings_service.get_ocr_concurrency",
            return_value=6,
        ) as get_conc,
        patch(
            "app.services.ingestion.chandra_extractor.extract_with_chandra",
            return_value=fake_result,
        ) as extract,
    ):
        result = convert_file("doc.pdf", engine="chandra")

    get_conc.assert_called_once_with(fake_session)
    extract.assert_called_once()
    _, kwargs = extract.call_args
    assert kwargs["max_workers"] == 6
    assert result == fake_result
    fake_session.close.assert_called_once()


@pytest.mark.unit
def test_convert_file_docling_engine_never_touches_ocr_concurrency():
    """Non-Chandra engine must not import/call get_ocr_concurrency at all."""
    with patch("app.services.user_settings_service.get_ocr_concurrency") as get_conc:
        with patch(
            "app.services.ingestion.converters._convert_in_subprocess",
            return_value={"content": "x", "metadata": {"pages": 1}, "chunks": []},
        ):
            convert_file("doc.pdf", engine="docling")
    get_conc.assert_not_called()
