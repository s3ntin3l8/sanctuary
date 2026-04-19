from app.services.ingestion.email_parser import parse_rfc822


def test_parse_rfc822():
    raw_email = b"From: test@example.com\nSubject: Test Email\nMessage-ID: <123@mail>\nDate: Mon, 1 Jan 2026 12:00:00 +0000\n\nBody content."
    result = parse_rfc822(raw_email)
    assert result["sender"] == "test@example.com"
    assert result["subject"] == "Test Email"
    assert result["message_id"] == "<123@mail>"
    assert result["body"].strip() == "Body content."
    assert len(result["attachments"]) == 0
