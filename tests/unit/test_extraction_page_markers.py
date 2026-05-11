from unittest.mock import MagicMock, patch

import pytest

from app.services.ingestion.converters import (
    _convert_in_subprocess,
    _run_conversion,
    is_valid_docling_output,
)


@pytest.mark.unit
def test_page_markers_injected():
    mock_conv = MagicMock()
    mock_res = MagicMock()
    mock_doc = MagicMock()

    # Simulate a 2-page document
    mock_doc.pages = [MagicMock(), MagicMock()]

    # Mock return value for export_to_markdown when called with page_break_placeholder
    def side_effect(page_break_placeholder=None):
        if page_break_placeholder == "<!-- PAGE_BREAK -->":
            return "Page 1 content<!-- PAGE_BREAK -->Page 2 content"
        return "Page 1 contentPage 2 content"

    mock_doc.export_to_markdown.side_effect = side_effect
    mock_res.document = mock_doc
    mock_res.input.format.value = "PDF"
    mock_conv.convert.return_value = mock_res

    markdown, chunks, metadata = _run_conversion(mock_conv, "test.pdf")

    assert "--- PAGE 1 ---" in markdown
    assert "--- PAGE 2 ---" in markdown
    assert "Page 1 content" in markdown
    assert "Page 2 content" in markdown
    assert markdown.index("--- PAGE 1 ---") < markdown.index("--- PAGE 2 ---")
    assert metadata["pages"] == 2


@pytest.mark.unit
def test_placeholder_re_ignores_page_markers():
    # Heuristic: _PLACEHOLDER_RE = re.compile(r"<!--[^>]*-->|--- PAGE \d+ ---")
    # cleaned = _PLACEHOLDER_RE.sub("", stripped)
    # word_chars = len(re.sub(r"\s+", "", cleaned))
    # return word_chars >= 30

    content = "--- PAGE 1 ---\n\nShort text"
    # "Shorttext" is 9 chars. < 30.
    assert is_valid_docling_output(content) is False

    content_long = (
        "--- PAGE 1 ---\n\n"
        + "This is a long enough text that should pass the validation check."
    )
    assert is_valid_docling_output(content_long) is True


@pytest.mark.unit
def test_ocr_fallback_metadata_set():
    with (
        patch("app.services.ingestion.converters._get_converter"),
        patch("app.services.ingestion.converters._get_ocr_converter"),
        patch("app.services.ingestion.converters._run_conversion") as mock_run,
    ):
        # Pass 1: returns image placeholders
        # _is_image_only_output returns True if has_placeholders and word_chars < 30
        mock_run.side_effect = [
            ("<!-- image -->", [], {"pages": 1}),  # Pass 1
            ("--- PAGE 1 ---\n\nReal text", [], {"pages": 1}),  # Pass 2
        ]

        result = _convert_in_subprocess("test.pdf")
        assert result.get("metadata", {}).get("ocr_fallback") is True
        assert "Real text" in result["content"]


@pytest.mark.unit
def test_no_ocr_fallback_for_short_text_with_page_markers():
    with (
        patch("app.services.ingestion.converters._get_converter"),
        patch("app.services.ingestion.converters._get_ocr_converter"),
        patch("app.services.ingestion.converters._run_conversion") as mock_run,
    ):
        # Even if text is short (< 30 chars), if there are NO image placeholders
        # (<!-- ... -->), it should NOT retry with OCR fallback.
        # It should just return the short text.
        short_content = "--- PAGE 1 ---\n\nShort native text"
        mock_run.return_value = (short_content, [], {"pages": 1})

        result = _convert_in_subprocess("test.pdf")

        # Should NOT have ocr_fallback flag
        assert result.get("metadata", {}).get("ocr_fallback") is not True
        assert result["content"] == short_content
        # _run_conversion should only have been called once
        assert mock_run.call_count == 1
