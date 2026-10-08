"""convert_file's .eml branch reads emails through the shared RFC 822 parser."""

import pytest

from app.services.ingestion.converters import convert_file


@pytest.mark.unit
def test_eml_renders_headers_and_body(tmp_path):
    eml = tmp_path / "msg.eml"
    eml.write_bytes(
        b"From: a@example.com\r\n"
        b"To: b@example.com\r\n"
        b"Subject: Hello\r\n"
        b"Date: Mon, 05 Oct 2026 10:00:00 +0000\r\n"
        b"Content-Type: text/plain; charset=utf-8\r\n\r\n"
        b"Body text here.\r\n"
    )

    result = convert_file(str(eml))

    assert "Subject: Hello" in result["content"]
    assert "From: a@example.com" in result["content"]
    assert "To: b@example.com" in result["content"]
    assert "Body text here." in result["content"]
    assert result["metadata"] == {"pages": 1}
