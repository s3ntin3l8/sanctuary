"""OCR text extraction for slicing: rapidocr 3.x output shape and failure logging."""

import logging
from types import SimpleNamespace

import pytest
from PIL import Image, ImageDraw, ImageFont

from app.services.ingestion import ocr_crosscheck, slicer


def test_ocr_page_text_joins_txts(monkeypatch):
    monkeypatch.setattr(
        ocr_crosscheck,
        "get_ocr",
        lambda: lambda arr: SimpleNamespace(txts=("Seite 2", "Mit")),
    )
    assert slicer._ocr_page_text(Image.new("RGB", (10, 10))) == "Seite 2 Mit"


def test_ocr_page_text_blank_page(monkeypatch):
    monkeypatch.setattr(
        ocr_crosscheck, "get_ocr", lambda: lambda arr: SimpleNamespace(txts=None)
    )
    assert slicer._ocr_page_text(Image.new("RGB", (10, 10))) == ""


def test_ocr_page_text_failure_is_logged(monkeypatch, caplog):
    def boom():
        raise RuntimeError("engine gone")

    monkeypatch.setattr(ocr_crosscheck, "get_ocr", boom)
    with caplog.at_level(logging.WARNING, logger=slicer.logger.name):
        assert slicer._ocr_page_text(Image.new("RGB", (10, 10))) == ""
    assert "engine gone" in caplog.text


def test_ocr_reads_rendered_text():
    """Real engine: catches rapidocr API changes the mocks above cannot."""
    pytest.importorskip("rapidocr")
    img = Image.new("RGB", (900, 160), "white")
    draw = ImageDraw.Draw(img)
    try:
        font = ImageFont.load_default(size=48)
    except TypeError:
        pytest.skip("Pillow too old for scalable default font")
    draw.text((20, 50), "Sehr geehrte Damen und Herren", fill="black", font=font)
    assert "geehrte" in slicer._ocr_page_text(img)
