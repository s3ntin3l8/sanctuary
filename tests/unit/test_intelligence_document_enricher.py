"""Tests for Phase 4 document enricher."""

from unittest.mock import patch

import pytest

from app.models.database import Document
from app.models.enums import (
    DocumentRole,
    DocumentType,
    OriginatorType,
    SignificanceTier,
)
from app.services.intelligence.document_enricher import (
    _apply_enrichment,
    _call_enricher_sync,
)


@pytest.fixture
def doc_with_content(db_session, sample_case):
    doc = Document(
        title="Test Ruling",
        content="Das Gericht ordnet an, dass die Klage abgewiesen wird.",
        case_id=sample_case.id,
        originator_type=OriginatorType.COURT,
        role=DocumentRole.STANDALONE,
    )
    db_session.add(doc)
    db_session.commit()
    db_session.refresh(doc)
    return doc


@pytest.mark.unit
def test_apply_enrichment_populates_fields(doc_with_content):
    result = {
        "significance_tier": "critical",
        "document_type": "ruling",
        "key_passages": [
            {"text": "Das Gericht ordnet an", "rationale": "core ruling", "span": "p1"}
        ],
        "cost_delta": {
            "amount": 450.50,
            "direction": "incoming",
            "description": "Court fee",
        },
        "management_summary": {
            "legal_significance": "Court dismissed the case.",
            "required_action": "File appeal by 2025-03-01.",
            "financial_impact": "450.50 EUR court fee due.",
        },
    }

    _apply_enrichment(doc_with_content, result)

    assert doc_with_content.significance_tier == SignificanceTier.CRITICAL
    assert doc_with_content.document_type == DocumentType.RULING
    assert doc_with_content.key_passages[0]["text"] == "Das Gericht ordnet an"
    assert doc_with_content.cost_delta["amount"] == 450.50
    assert doc_with_content.cost_delta["direction"] == "incoming"
    assert (
        doc_with_content.ai_summary["legal_significance"] == "Court dismissed the case."
    )
    assert (
        doc_with_content.ai_summary["required_action"] == "File appeal by 2025-03-01."
    )
    assert (
        doc_with_content.ai_summary["financial_impact"] == "450.50 EUR court fee due."
    )
    assert doc_with_content.ai_summary is not None


@pytest.mark.unit
def test_ai_summary_has_required_keys(doc_with_content):
    """Template contract: doc.ai_summary must have exactly these three keys."""
    result = {
        "significance_tier": "significant",
        "document_type": "motion",
        "key_passages": [],
        "cost_delta": None,
        "management_summary": {
            "legal_significance": "Motion filed.",
            "required_action": "Respond by 2025-04-01.",
            "financial_impact": "None",
        },
    }

    _apply_enrichment(doc_with_content, result)

    required_keys = {"legal_significance", "required_action", "financial_impact"}
    assert set(doc_with_content.ai_summary.keys()) == required_keys


@pytest.mark.unit
def test_thread_open_set_for_statement(doc_with_content):
    result = {
        "significance_tier": "significant",
        "document_type": "statement",
        "key_passages": [],
        "cost_delta": None,
        "management_summary": {
            "legal_significance": "Statement filed.",
            "required_action": "Review.",
            "financial_impact": "None",
        },
    }

    _apply_enrichment(doc_with_content, result)

    assert doc_with_content.thread_open is True


@pytest.mark.unit
def test_thread_open_false_for_ruling(doc_with_content):
    result = {
        "significance_tier": "critical",
        "document_type": "ruling",
        "key_passages": [],
        "cost_delta": None,
        "management_summary": {
            "legal_significance": "Ruling issued.",
            "required_action": "Appeal if needed.",
            "financial_impact": "None",
        },
    }

    _apply_enrichment(doc_with_content, result)

    assert doc_with_content.thread_open is False


@pytest.mark.unit
def test_invalid_cost_delta_direction_normalized(doc_with_content):
    """Invalid direction must be normalized to 'none', not crash."""
    result = {
        "significance_tier": "informational",
        "document_type": "correspondence",
        "key_passages": [],
        "cost_delta": {
            "amount": 100.0,
            "direction": "payment_due",
            "description": "Fee",
        },
        "management_summary": {
            "legal_significance": "x",
            "required_action": "x",
            "financial_impact": "x",
        },
    }

    _apply_enrichment(doc_with_content, result)

    assert doc_with_content.cost_delta["direction"] == "none"
    assert doc_with_content.cost_delta["amount"] == 100.0


@pytest.mark.unit
def test_call_enricher_sync_includes_cover_letter_context(db_session, doc_with_content):
    """When doc.role=COVER_LETTER, the user prompt must carry the cover-letter
    batch context line so the model titles + classifies it as a cover letter
    rather than as the subject of its attachment.
    """
    doc_with_content.role = DocumentRole.COVER_LETTER
    db_session.commit()
    db_session.refresh(doc_with_content)

    captured = {}

    def fake_call_json_ai(*args, **kwargs):
        captured["user_prompt"] = kwargs.get("user_prompt", "")
        return {}

    with patch(
        "app.services.intelligence.document_enricher.call_json_ai",
        side_effect=fake_call_json_ai,
    ):
        _call_enricher_sync(doc_with_content)

    assert "Batch context: This document is a cover letter" in captured["user_prompt"]
    assert "document_type='relay'" in captured["user_prompt"]
    assert "significance_tier='administrative'" in captured["user_prompt"]


@pytest.mark.unit
def test_call_enricher_sync_includes_enclosure_context(db_session, doc_with_content):
    """ENCLOSURE role + attributed_originator yields the existing enclosure context line."""
    doc_with_content.role = DocumentRole.ENCLOSURE
    doc_with_content.attributed_originator = "Kanzlei Müller & Partner"
    db_session.commit()
    db_session.refresh(doc_with_content)

    captured = {}

    def fake_call_json_ai(*args, **kwargs):
        captured["user_prompt"] = kwargs.get("user_prompt", "")
        return {}

    with patch(
        "app.services.intelligence.document_enricher.call_json_ai",
        side_effect=fake_call_json_ai,
    ):
        _call_enricher_sync(doc_with_content)

    assert (
        "Batch context: This document was enclosed in a cover letter"
        in captured["user_prompt"]
    )
    assert "Kanzlei Müller & Partner" in captured["user_prompt"]


@pytest.mark.unit
def test_call_enricher_sync_no_batch_context_for_standalone(
    db_session, doc_with_content
):
    """STANDALONE docs get no batch context line."""
    captured = {}

    def fake_call_json_ai(*args, **kwargs):
        captured["user_prompt"] = kwargs.get("user_prompt", "")
        return {}

    with patch(
        "app.services.intelligence.document_enricher.call_json_ai",
        side_effect=fake_call_json_ai,
    ):
        _call_enricher_sync(doc_with_content)

    assert "Batch context:" not in captured["user_prompt"]


@pytest.mark.unit
def test_malformed_ai_response_propagates_exception(db_session, doc_with_content):
    """AI call errors must propagate from enrich() so the Celery wrapper can mark_failed."""
    import pytest

    with (
        patch(
            "app.services.intelligence.document_enricher.SessionLocal",
            return_value=db_session,
        ),
        patch.object(db_session, "close"),
        patch(
            "app.services.intelligence.document_enricher._call_enricher_sync",
            side_effect=ValueError("Malformed JSON"),
        ),
        pytest.raises(ValueError, match="Malformed JSON"),
    ):
        from app.services.intelligence.document_enricher import enrich

        enrich(doc_with_content.id)
