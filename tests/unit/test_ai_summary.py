from datetime import UTC, datetime
from unittest.mock import patch

import pytest

from app.models.database import Document
from app.models.enums import OriginatorType
from app.services.ai_summary import (
    _summarize_document_sync,
    generate_summary_sync,
    get_content_preview,
)


@pytest.mark.unit
def test_summarize_document_sync_success(db_session, sample_document):
    with patch("app.services.ai_summary.generate_summary_sync") as mock_gen:
        # Phase 1 returns metadata-only keys; 3-bullet summary now comes from Phase 4 enricher
        mock_gen.return_value = {
            "az_court": "003 F 426/25",
            "sender": "Amtsgericht Hamburg",
            "date": "2025-01-15",
            "originator": "court",
        }

        updated_doc = _summarize_document_sync(sample_document.id, db_session)

        # Phase 1 does NOT set ai_summary (that's Phase 4's job)
        assert updated_doc.ai_summary is None or "error" not in updated_doc.ai_summary


@pytest.mark.unit
def test_summarize_document_sync_failure(db_session, sample_document):
    with patch("app.services.ai_summary.generate_summary_sync") as mock_gen:
        mock_gen.side_effect = Exception("Ollama Error")

        with pytest.raises(Exception, match="Ollama Error"):
            _summarize_document_sync(sample_document.id, db_session)

        updated_doc = db_session.get(Document, sample_document.id)

        assert updated_doc.ai_summary is not None
        assert "Ollama Error" in updated_doc.ai_summary["error"]


@pytest.mark.unit
def test_get_content_preview_short_doc(sample_document):
    """Short docs returned as-is, no truncation."""
    sample_document.content = "Short content"
    sample_document.meta = {}
    result = get_content_preview(sample_document, max_chars=4000)
    assert result == "Short content"


@pytest.mark.unit
def test_get_content_preview_long_doc_head_tail(sample_document):
    """Long docs get head+tail window."""
    head = "A" * 3000
    tail = "Z" * 2000
    middle = "M" * 5000
    sample_document.content = head + middle + tail
    sample_document.meta = {}
    result = get_content_preview(sample_document, max_chars=4000)
    assert result.startswith("A" * 2000)  # head portion (4000 - 2000 = 2000 head chars)
    assert "[... truncated middle ...]" in result
    assert result.endswith("Z" * 2000)  # last 2000 chars exactly


@pytest.mark.unit
def test_get_content_preview_no_tail(sample_document):
    """include_tail=False returns head-only even for long docs."""
    sample_document.content = "A" * 10000
    sample_document.meta = {}
    result = get_content_preview(sample_document, max_chars=4000, include_tail=False)
    assert result == "A" * 4000
    assert "[... truncated middle ...]" not in result


# --- 3b: hint-filtering prompt tests ---


@pytest.mark.unit
def test_generate_summary_sync_strips_null_hints(db_session):
    doc = Document(
        title="Null Hint Test",
        content="Betreff: Klage vor dem Amtsgericht Hamburg",
        sender="ag.hamburg@justiz.de",
        received_date=datetime(2025, 3, 11, tzinfo=UTC),
        originator_type=OriginatorType.COURT,
    )
    db_session.add(doc)
    db_session.commit()

    captured = {}

    def fake_call_json_ai(**kwargs):
        captured["user_prompt"] = kwargs.get("user_prompt", "")
        return {
            "az_court": None,
            "internal_id": None,
            "sender": None,
            "date": None,
            "originator": None,
            "confidence": {},
        }

    with patch("app.services.ai_summary.call_json_ai", side_effect=fake_call_json_ai):
        generate_summary_sync(doc, db=db_session)

    prompt = captured["user_prompt"]
    if "### Heuristic Hints" in prompt:
        hints_section = prompt.split("### Heuristic Hints")[1].split("### Document")[0]
        assert "null" not in hints_section.lower(), (
            "Null values should not appear in hint block"
        )


@pytest.mark.unit
def test_generate_summary_sync_omits_hint_block_when_all_null(db_session):
    doc = Document(
        title="No Hints Test",
        content="",
        sender=None,
        received_date=None,
        originator_type=None,
    )
    db_session.add(doc)
    db_session.commit()

    captured = {}

    def fake_call_json_ai(**kwargs):
        captured["user_prompt"] = kwargs.get("user_prompt", "")
        return {
            "az_court": None,
            "internal_id": None,
            "sender": None,
            "date": None,
            "originator": None,
            "confidence": {},
        }

    with patch("app.services.ai_summary.call_json_ai", side_effect=fake_call_json_ai):
        generate_summary_sync(doc, db=db_session)

    assert "### Heuristic Hints" not in captured.get("user_prompt", "")
