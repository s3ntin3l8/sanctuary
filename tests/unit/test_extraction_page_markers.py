from unittest.mock import MagicMock, patch

import pytest

from app.services.ingestion.converters import (
    _apply_glyph_fixes,
    _collect_pictures,
    _convert_in_subprocess,
    _extract_pdf_text_layer,
    _layout_model_spec,
    _ocr_with_rotation_correction,
    _run_conversion,
    _substitute_picture_placeholders,
    is_valid_docling_output,
)


def _fake_picture(page_no: int, left=0.0, top=100.0, right=100.0, bottom=0.0):
    """Build a MagicMock that mimics a docling PictureItem with one provenance entry."""
    prov = MagicMock()
    prov.page_no = page_no
    prov.bbox.l = left
    prov.bbox.t = top
    prov.bbox.r = right
    prov.bbox.b = bottom
    pic = MagicMock()
    pic.prov = [prov]
    return pic


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
def test_collect_pictures_groups_by_page_in_order():
    doc = MagicMock()
    p1 = _fake_picture(1)
    p2a = _fake_picture(2)
    p2b = _fake_picture(2)
    doc.pictures = [p1, p2a, p2b]
    by_page = _collect_pictures(doc)
    assert list(by_page.keys()) == [1, 2]
    assert by_page[1] == [p1]
    assert by_page[2] == [p2a, p2b]


@pytest.mark.unit
def test_collect_pictures_skips_pictures_without_provenance():
    doc = MagicMock()
    good = _fake_picture(1)
    bad = MagicMock()
    bad.prov = []
    doc.pictures = [good, bad]
    by_page = _collect_pictures(doc)
    assert list(by_page.keys()) == [1]
    assert by_page[1] == [good]


@pytest.mark.unit
def test_substitute_placeholders_replaces_in_reading_order():
    md = "Before\n\n<!-- image -->\n\nMiddle\n\n<!-- image -->\n\nAfter"
    result = _substitute_picture_placeholders(md, ["FIRST", "SECOND"])
    assert "<!-- image -->" not in result
    assert result.index("FIRST") < result.index("SECOND")
    assert result.index("Before") < result.index("FIRST") < result.index("Middle")
    assert result.index("Middle") < result.index("SECOND") < result.index("After")


@pytest.mark.unit
def test_substitute_keeps_placeholder_when_text_empty():
    md = "<!-- image -->"
    result = _substitute_picture_placeholders(md, [""])
    assert result == "<!-- image -->"


@pytest.mark.unit
def test_substitute_count_mismatch_appends_at_tail(caplog):
    import logging

    md = "Body\n\n<!-- image -->"  # only 1 placeholder, 2 recovered texts
    with caplog.at_level(logging.WARNING, logger="app.services.ingestion.converters"):
        result = _substitute_picture_placeholders(md, ["RECOVERED-A", "RECOVERED-B"])
    assert "RECOVERED-A" in result
    assert "RECOVERED-B" in result
    assert any("placeholder count mismatch" in r.message for r in caplog.records)


@pytest.mark.unit
def test_run_conversion_recovers_picture_text_from_pdf_layer():
    """Picture region with embedded text gets that text substituted at placeholder site."""
    mock_conv = MagicMock()
    mock_res = MagicMock()
    mock_doc = MagicMock()
    mock_doc.pages = [MagicMock()]
    mock_doc.pictures = [_fake_picture(1)]
    # Body must be long enough that the page isn't treated as image-only —
    # otherwise the sandwich-PDF gate would (correctly) skip picture recovery.
    body = (
        "The court has reviewed the matter and finds the following. Judges named above"
    )
    mock_doc.export_to_markdown.return_value = f"{body}\n\n<!-- image -->"
    mock_res.document = mock_doc
    mock_res.input.format.value = "PDF"
    mock_conv.convert.return_value = mock_res

    with patch(
        "app.services.ingestion.converters._recover_picture_text"
    ) as mock_recover:
        # Map by id(picture) — match what _recover_picture_text returns
        mock_recover.return_value = {id(mock_doc.pictures[0]): "Für die Richtigkeit"}
        md, _chunks, meta = _run_conversion(mock_conv, "stamp.pdf")

    assert "<!-- image -->" not in md
    assert "Für die Richtigkeit" in md
    # Position check: appears where the placeholder was, after judges
    assert md.index("Judges named above") < md.index("Für die Richtigkeit")
    assert meta.get("picture_text_recovered") == 1


@pytest.mark.unit
def test_run_conversion_leaves_placeholder_when_recovery_empty():
    """Picture with no text layer and no OCR result keeps its <!-- image --> placeholder."""
    mock_conv = MagicMock()
    mock_res = MagicMock()
    mock_doc = MagicMock()
    mock_doc.pages = [MagicMock()]
    mock_doc.pictures = [_fake_picture(1)]
    body = "Body content long enough to clear the sandwich-PDF threshold and exercise picture recovery."
    mock_doc.export_to_markdown.return_value = f"{body}\n\n<!-- image -->"
    mock_res.document = mock_doc
    mock_res.input.format.value = "PDF"
    mock_conv.convert.return_value = mock_res

    with patch(
        "app.services.ingestion.converters._recover_picture_text"
    ) as mock_recover:
        mock_recover.return_value = {id(mock_doc.pictures[0]): ""}
        md, _chunks, meta = _run_conversion(mock_conv, "decorative.pdf")

    assert "<!-- image -->" in md
    assert "picture_text_recovered" not in meta


@pytest.mark.unit
def test_run_conversion_skips_picture_recovery_for_non_pdf():
    """Non-PDF input never invokes pypdfium2 picture recovery."""
    mock_conv = MagicMock()
    mock_res = MagicMock()
    mock_doc = MagicMock()
    mock_doc.pages = [MagicMock()]
    mock_doc.pictures = [_fake_picture(1)]
    mock_doc.export_to_markdown.return_value = "Body\n\n<!-- image -->"
    mock_res.document = mock_doc
    mock_res.input.format.value = "DOCX"
    mock_conv.convert.return_value = mock_res

    with patch(
        "app.services.ingestion.converters._recover_picture_text"
    ) as mock_recover:
        md, _chunks, meta = _run_conversion(mock_conv, "letter.docx")

    mock_recover.assert_not_called()
    assert "<!-- image -->" in md
    assert "picture_text_recovered" not in meta


@pytest.mark.unit
def test_recover_picture_text_uses_text_layer_first_then_ocr():
    """When the PDF text layer has content under the bbox, OCR is not invoked.
    When it doesn't, OCR fallback runs."""
    from app.services.ingestion.converters import _recover_picture_text

    page1_pic = _fake_picture(1)
    page2_pic = _fake_picture(2)
    pictures_by_page = {1: [page1_pic], 2: [page2_pic]}

    mock_page1 = MagicMock()
    mock_page2 = MagicMock()
    mock_pdf = MagicMock()
    mock_pdf.__getitem__.side_effect = lambda i: [mock_page1, mock_page2][i]

    with (
        patch("pypdfium2.PdfDocument", return_value=mock_pdf),
        patch("app.services.ingestion.converters._pdf_text_in_bbox") as mock_text,
        patch("app.services.ingestion.converters._ocr_picture_region") as mock_ocr,
    ):
        # Page 1 has text under bbox; page 2 does not
        mock_text.side_effect = ["Page 1 stamp text", ""]
        mock_ocr.return_value = "Page 2 OCR text"

        result = _recover_picture_text("any.pdf", pictures_by_page)

    assert result[id(page1_pic)] == "Page 1 stamp text"
    assert result[id(page2_pic)] == "Page 2 OCR text"
    # OCR called exactly once — only for the picture with no text layer
    assert mock_ocr.call_count == 1


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


# ---------------------------------------------------------------------------
# Round 2: layout model + glyph-fixes cleanups
# ---------------------------------------------------------------------------


@pytest.mark.unit
def test_unknown_placeholder_stripped():
    """Both literal and HTML-escaped <unknown> placeholders are removed."""
    md_in = "Body text.\n\n<unknown>\n\nMore body.\n\n&lt;unknown&gt;\n\nEnd."
    out = _apply_glyph_fixes(md_in)
    assert "<unknown>" not in out
    assert "&lt;unknown&gt;" not in out
    assert "Body text." in out and "More body." in out and "End." in out


@pytest.mark.unit
def test_amp_entity_unescaped():
    """Plain-text HTML entities Docling injects get unescaped."""
    out = _apply_glyph_fixes("Rechtsanwälte Pietsch &amp; Hönig")
    assert out == "Rechtsanwälte Pietsch & Hönig"


@pytest.mark.unit
def test_lt_gt_entities_unescaped():
    """&lt;tag&gt; entities outside the unknown-placeholder pattern still get
    unescaped (no separate codepath)."""
    # Use a tag whose body is not "unknown" so the strip step leaves it alone.
    out = _apply_glyph_fixes("See &lt;Anlage 3&gt; for details.")
    assert out == "See <Anlage 3> for details."


@pytest.mark.unit
def test_phantom_prefix_stripped():
    """Phantom auto-numbering on legal sub-items (4. 2.b.a)) is stripped,
    real numbered headings (4. Body sentence) are preserved."""
    md_in = (
        "Some context.\n\n"
        "4. 2.b.a)  Das betrifft auch das Aufsuchen.\n\n"
        "4. Body sentence that is a real list item.\n"
    )
    out = _apply_glyph_fixes(md_in)
    assert "2.b.a)  Das betrifft auch das Aufsuchen." in out
    assert "4. 2.b.a)" not in out
    # Real list item untouched
    assert "4. Body sentence that is a real list item." in out


@pytest.mark.unit
def test_leading_single_char_noise_stripped():
    """Document-head punctuation/letter scraps (`:`, `u`, `'`, `|`, `|`)
    from Jugendamt form templates are stripped; legitimate short markers
    deeper in the doc are preserved."""
    md_in = (
        ":\n\n"
        "u\n\n"
        "'\n\n"
        "|\n\n"
        "|\n\n"
        "Landratsamt Eichstätt — Amt für Familie und Jugend\n\n"
        "Body text after the noise.\n"
    )
    out = _apply_glyph_fixes(md_in)
    assert out.startswith("Landratsamt Eichstätt")
    assert "Body text after the noise." in out


@pytest.mark.unit
def test_leading_noise_strip_preserves_roman_numeral_markers():
    """A short legitimate marker like 'I.' at the head should NOT be stripped
    when it isn't pure single-char noise."""
    md_in = "I.\n\nDie Antragstellerin trägt vor, dass …\n"
    out = _apply_glyph_fixes(md_in)
    # I. is two non-noise chars (letter + period); should survive
    assert out.startswith("I.")
    assert "Die Antragstellerin" in out


@pytest.mark.unit
def test_layout_model_configured_to_egret_large():
    """Layout model is set to EGRET_LARGE via the lazily-imported spec.
    Defensive: catches accidental model swap in PRs."""
    from docling.datamodel.pipeline_options import DOCLING_LAYOUT_EGRET_LARGE

    assert _layout_model_spec() is DOCLING_LAYOUT_EGRET_LARGE


@pytest.mark.unit
def test_run_conversion_skips_picture_recovery_on_image_only_output():
    """When the standard docling pass produces only image placeholders (the
    sandwich-PDF signal), picture recovery is skipped so the whole-document
    text-layer fallback in _convert_in_subprocess can handle it cleanly."""
    mock_conv = MagicMock()
    mock_res = MagicMock()
    mock_doc = MagicMock()
    mock_doc.pages = [MagicMock()]
    mock_doc.pictures = [_fake_picture(1)]
    # All placeholders, no real text — looks like a scan
    mock_doc.export_to_markdown.return_value = "<!-- image -->\n\n<!-- image -->"
    mock_res.document = mock_doc
    mock_res.input.format.value = "PDF"
    mock_conv.convert.return_value = mock_res

    with patch(
        "app.services.ingestion.converters._recover_picture_text"
    ) as mock_recover:
        md, _chunks, meta = _run_conversion(mock_conv, "scanned.pdf")

    mock_recover.assert_not_called()
    assert "picture_text_recovered" not in meta
    # Placeholders preserved so the outer sandwich gate still sees image-only
    assert "<!-- image -->" in md
