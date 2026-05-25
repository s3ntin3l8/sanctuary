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
            "originator_type": "court",
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
def test_get_content_preview_long_doc_proportional(sample_document):
    """Long docs get proportional 25/50/25 window."""
    # 1000 chars total: 0123456789...
    content = "".join([str(i % 10) for i in range(1000)])
    sample_document.content = content
    sample_document.meta = {}

    # Request 400 chars: 100 head, 200 middle, 100 tail
    result = get_content_preview(sample_document, max_chars=400)

    separator = "[... Omitted for brevity ...]"
    assert separator in result

    # Head (25% of 400 = 100)
    assert result.startswith(content[:100])

    # Tail (25% of 400 = 100)
    assert result.endswith(content[-100:])

    # Middle (50% of 400 = 200)
    # Content mid is 500. Mid-size is 200. Start = 500 - 100 = 400.
    mid_content = content[400:600]
    assert mid_content in result


@pytest.mark.unit
def test_get_content_preview_no_tail(sample_document):
    """include_tail=False is now removed, proportional windowing used."""
    sample_document.content = "A" * 10000
    sample_document.meta = {}
    result = get_content_preview(sample_document, max_chars=4000)
    assert "[... Omitted for brevity ...]" in result


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
        from app.services.intelligence.schemas import Phase1Metadata

        captured["user_prompt"] = kwargs.get("user_prompt", "")
        return Phase1Metadata.model_validate(
            {
                "az_court": None,
                "internal_id": None,
                "sender": None,
                "originator_type": None,
                "confidence": {},
            }
        )

    with patch("app.services.ai_summary.call_json_ai", side_effect=fake_call_json_ai):
        generate_summary_sync(doc, db=db_session)

    prompt = captured["user_prompt"]
    if "### Heuristic Hints" in prompt:
        hints_section = prompt.split("### Heuristic Hints")[1].split("### Document")[0]
        assert "null" not in hints_section.lower(), (
            "Null values should not appear in hint block"
        )


@pytest.mark.unit
def test_enrich_document_tracks_strategy(db_session, sample_document):
    """Enrichment should track if full or windowed strategy was used."""
    from app.services.ai_summary import enrich_document_with_ai

    # 1. Full strategy
    sample_document.content = "Short content"
    enrich_document_with_ai(sample_document, {"confidence": {}}, db_session)
    assert sample_document.meta["ai_context_strategy"] == "full"
    assert sample_document.meta["ai_context_chars"] == 13

    # 2. Windowed strategy
    sample_document.content = "A" * 70000
    enrich_document_with_ai(sample_document, {"confidence": {}}, db_session)
    assert sample_document.meta["ai_context_strategy"] == "windowed"
    # 60k chars + 2 separators (approx 33 chars each)
    assert sample_document.meta["ai_context_chars"] > 60000


# ---------------------------------------------------------------------------
# Sender sanitizer: strip Docling markdown image alt-text bleeding into sender
# ---------------------------------------------------------------------------


@pytest.mark.unit
def test_sender_sanitizer_strips_markdown_image_alt_text():
    """Doc-4 pattern: the AI copied a Docling-rendered image alt-text into
    `sender` verbatim. The sanitizer strips `![…](…)` so a real sender
    fragment (or nothing) is what reaches the DB."""
    from app.services.ai_summary import _sanitize_sender

    raw = (
        "![Red stamp of Amtsgericht Ingolstadt, dated 14. Nov. 2025, "
        "with the text 'Nachbriefkasten' at the bottom.]()"
        "Amtsgericht Ingolstadt"
    )
    assert _sanitize_sender(raw) == "Amtsgericht Ingolstadt"


@pytest.mark.unit
def test_sender_sanitizer_returns_none_when_string_was_only_alt_text():
    """When the entire sender string is image alt-text, return None — the
    apply layer then leaves doc.sender unchanged instead of overwriting a
    good prior value with nothing."""
    from app.services.ai_summary import _sanitize_sender

    raw = "![Red stamp of Amtsgericht Ingolstadt, ...](https://x/img.png)"
    assert _sanitize_sender(raw) is None


@pytest.mark.unit
def test_sender_sanitizer_passes_clean_sender_through():
    """A normal sender string is returned unchanged (modulo whitespace)."""
    from app.services.ai_summary import _sanitize_sender

    assert _sanitize_sender("Haidl Funk Rechtsanwälte") == "Haidl Funk Rechtsanwälte"
    assert _sanitize_sender("  Amtsgericht Ingolstadt  ") == "Amtsgericht Ingolstadt"


@pytest.mark.unit
def test_sender_sanitizer_handles_none_and_empty():
    """Edge cases: None and empty strings round-trip without exception."""
    from app.services.ai_summary import _sanitize_sender

    assert _sanitize_sender(None) is None
    assert _sanitize_sender("") == ""


@pytest.mark.unit
def test_enrich_document_with_ai_sanitizes_sender(db_session, sample_document):
    """Integration: enrich_document_with_ai strips markdown image alt-text
    before writing to doc.sender."""
    from app.services.ai_summary import enrich_document_with_ai

    polluted = (
        "![Red stamp of Amtsgericht Ingolstadt, dated 14. Nov. 2025, "
        "with the text 'Nachbriefkasten' at the bottom.]()A red recta"
    )
    enrich_document_with_ai(
        sample_document,
        {"sender": polluted, "confidence": {}},
        db_session,
    )

    assert sample_document.sender == "A red recta", (
        f"sender should have markdown image stripped; got: {sample_document.sender!r}"
    )


@pytest.mark.unit
def test_enrich_document_with_ai_keeps_prior_sender_when_alt_only(
    db_session, sample_document
):
    """If the AI-emitted sender is *entirely* alt-text, the apply layer
    leaves the prior doc.sender unchanged (returning None from the sanitizer
    means "skip this write")."""
    from app.services.ai_summary import enrich_document_with_ai

    sample_document.sender = "Existing Clean Sender"
    db_session.commit()
    polluted = "![Red stamp of Amtsgericht Ingolstadt, ...](https://x/img.png)"
    enrich_document_with_ai(
        sample_document,
        {"sender": polluted, "confidence": {}},
        db_session,
    )

    assert sample_document.sender == "Existing Clean Sender"
