from unittest.mock import MagicMock, patch

import pytest

from app.services.ingestion.converters import (
    _convert_in_subprocess,
    _extract_pdf_text_layer,
    _ocr_with_rotation_correction,
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
    """When standard pass returns image-only and no text layer exists,
    _ocr_with_rotation_correction is called and ocr_fallback is set."""
    with (
        patch("app.services.ingestion.converters._get_converter"),
        patch("app.services.ingestion.converters._run_conversion") as mock_run,
        patch("app.services.ingestion.converters._extract_pdf_text_layer") as mock_tl,
        patch(
            "app.services.ingestion.converters._ocr_with_rotation_correction"
        ) as mock_rot,
    ):
        mock_run.return_value = ("<!-- image -->", [], {"pages": 1})
        mock_tl.return_value = None
        mock_rot.return_value = (
            "--- PAGE 1 ---\n\nReal text",
            [],
            {"pages": 1, "ocr_fallback": True},
        )

        result = _convert_in_subprocess("test.pdf")
        assert result.get("metadata", {}).get("ocr_fallback") is True
        assert "Real text" in result["content"]
        mock_rot.assert_called_once_with("test.pdf")


@pytest.mark.unit
def test_sandwich_pdf_uses_text_layer_not_ocr():
    """When the standard pass returns image-only output but a text layer exists,
    use it directly and set pdf_text_layer instead of ocr_fallback."""
    with (
        patch("app.services.ingestion.converters._get_converter"),
        patch("app.services.ingestion.converters._get_ocr_converter") as mock_ocr_conv,
        patch("app.services.ingestion.converters._run_conversion") as mock_run,
        patch("app.services.ingestion.converters._extract_pdf_text_layer") as mock_tl,
    ):
        mock_run.return_value = ("<!-- image -->", [], {"pages": 1})
        mock_tl.return_value = "--- PAGE 1 ---\n\nSehr geehrte Damen und Herren,"

        result = _convert_in_subprocess("sandwich.pdf")

        assert result["metadata"].get("pdf_text_layer") is True
        assert result["metadata"].get("ocr_fallback") is None
        assert "Sehr geehrte Damen und Herren" in result["content"]
        # OCR converter must NOT have been called
        mock_ocr_conv.assert_not_called()
        # _run_conversion called only once (standard pass)
        assert mock_run.call_count == 1


@pytest.mark.unit
def test_no_text_layer_uses_rotation_corrected_ocr():
    """When no usable text layer is found, rotation-corrected OCR is used."""
    with (
        patch("app.services.ingestion.converters._get_converter"),
        patch("app.services.ingestion.converters._run_conversion") as mock_run,
        patch("app.services.ingestion.converters._extract_pdf_text_layer") as mock_tl,
        patch(
            "app.services.ingestion.converters._ocr_with_rotation_correction"
        ) as mock_rot,
    ):
        mock_run.return_value = ("<!-- image -->", [], {"pages": 1})
        mock_tl.return_value = None
        mock_rot.return_value = (
            "--- PAGE 1 ---\n\nOCR extracted text",
            [],
            {"pages": 1, "ocr_fallback": True},
        )

        result = _convert_in_subprocess("scanned.pdf")

        assert result["metadata"].get("ocr_fallback") is True
        assert result["metadata"].get("pdf_text_layer") is None
        mock_rot.assert_called_once_with("scanned.pdf")


@pytest.mark.unit
def test_extract_pdf_text_layer_returns_none_on_short_content():
    """_extract_pdf_text_layer returns None when extracted text is below threshold."""
    mock_page = MagicMock()
    mock_textpage = MagicMock()
    mock_textpage.get_text_range.return_value = "tiny"
    mock_page.get_textpage.return_value = mock_textpage

    mock_doc = MagicMock()
    mock_doc.__iter__ = MagicMock(return_value=iter([mock_page]))

    with patch("pypdfium2.PdfDocument", return_value=mock_doc):
        result = _extract_pdf_text_layer("any.pdf")

    assert result is None


@pytest.mark.unit
def test_extract_pdf_text_layer_formats_pages():
    """_extract_pdf_text_layer inserts --- PAGE N --- markers per page."""

    def make_page(text):
        tp = MagicMock()
        tp.get_text_range.return_value = text
        p = MagicMock()
        p.get_textpage.return_value = tp
        return p

    pages = [make_page("A" * 60), make_page("B" * 60)]
    mock_doc = MagicMock()
    mock_doc.__iter__ = MagicMock(return_value=iter(pages))

    with patch("pypdfium2.PdfDocument", return_value=mock_doc):
        result = _extract_pdf_text_layer("any.pdf")

    assert result is not None
    assert "--- PAGE 1 ---" in result
    assert "--- PAGE 2 ---" in result
    assert result.index("--- PAGE 1 ---") < result.index("--- PAGE 2 ---")


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


@pytest.mark.unit
def test_rotation_corrected_ocr_sets_metadata():
    """_ocr_with_rotation_correction sets ocr_fallback and ocr_rotation_corrected
    when a page has non-zero stored rotation."""
    mock_page = MagicMock()
    mock_page.get_rotation.return_value = 270  # typical Canon scanner output
    mock_bitmap = MagicMock()
    mock_img = MagicMock()
    mock_img.rotate.return_value = mock_img  # safety-net rotate call
    mock_bitmap.to_pil.return_value = mock_img
    mock_page.render.return_value = mock_bitmap

    mock_pdf = MagicMock()
    mock_pdf.__iter__ = MagicMock(return_value=iter([mock_page]))
    mock_pdf.__len__ = MagicMock(return_value=1)

    good_content = "--- PAGE 1 ---\n\nSehr geehrte Damen und Herren," + "x" * 100

    with (
        patch("pypdfium2.PdfDocument", return_value=mock_pdf),
        patch("app.services.ingestion.converters._get_image_ocr_converter"),
        patch("app.services.ingestion.converters._run_conversion") as mock_run,
    ):
        mock_run.return_value = (good_content, [], {"pages": 1, "format": "image"})

        result_md, result_chunks, result_meta = _ocr_with_rotation_correction(
            "scan.pdf"
        )

    assert result_meta["ocr_fallback"] is True
    assert result_meta.get("ocr_rotation_corrected") is True
    assert "Sehr geehrte Damen und Herren" in result_md
    # render was called with correction=(360-270)%360=90
    mock_page.render.assert_called_once()
    call_kwargs = mock_page.render.call_args
    assert call_kwargs.kwargs.get("rotation") == 90 or (
        len(call_kwargs.args) >= 2 and call_kwargs.args[1] == 90
    )


@pytest.mark.unit
def test_rotation_corrected_safety_net_retries_at_90():
    """When metadata-corrected render still yields image-only output,
    the safety net retries at +90° and uses that result if it has content."""
    mock_page = MagicMock()
    mock_page.get_rotation.return_value = 0  # no metadata rotation
    mock_bitmap = MagicMock()

    # PIL image mock: rotate(-90) returns a new mock with a save method
    mock_img = MagicMock()
    mock_img_rotated = MagicMock()
    mock_img.rotate.return_value = mock_img_rotated
    mock_bitmap.to_pil.return_value = mock_img
    mock_page.render.return_value = mock_bitmap

    mock_pdf = MagicMock()
    mock_pdf.__iter__ = MagicMock(return_value=iter([mock_page]))
    mock_pdf.__len__ = MagicMock(return_value=1)

    image_only = "<!-- image -->"
    good_content = "--- PAGE 1 ---\n\nGescannt aber lesbar" + "x" * 100

    with (
        patch("pypdfium2.PdfDocument", return_value=mock_pdf),
        patch("app.services.ingestion.converters._get_image_ocr_converter"),
        patch("app.services.ingestion.converters._run_conversion") as mock_run,
    ):
        # First call (metadata-corrected) → image-only; second (safety net +90°) → good
        mock_run.side_effect = [
            (image_only, [], {"pages": 1, "format": "image"}),
            (good_content, [], {"pages": 1, "format": "image"}),
        ]

        result_md, _, result_meta = _ocr_with_rotation_correction("scan.pdf")

    assert result_meta["ocr_fallback"] is True
    assert result_meta.get("ocr_rotation_corrected") is True
    assert "Gescannt aber lesbar" in result_md
    assert mock_run.call_count == 2
