"""Tests for Phase 4 document enricher."""

from unittest.mock import patch

import pytest

from app.models.database import Document
from app.models.enums import (
    DocumentRole,
    DocumentType,
    IngestStatus,
    OriginatorType,
    SignificanceTier,
)
from app.services.intelligence.document_enricher import _apply_enrichment


@pytest.fixture
def doc_with_content(db_session, sample_case):
    doc = Document(
        title="Test Ruling",
        content="Das Gericht ordnet an, dass die Klage abgewiesen wird.",
        case_id=sample_case.id,
        ingest_status=IngestStatus.COMPLETED,
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
    assert doc_with_content.ai_summary_status == "generated"


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
def test_malformed_ai_response_sets_failed_status(db_session, doc_with_content):
    """If enrich() catches an error, ai_summary_status must be 'failed' without crashing."""
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
    ):
        from app.services.intelligence.document_enricher import enrich

        enrich(doc_with_content.id)

    db_session.expire_all()
    doc = db_session.get(Document, doc_with_content.id)
    assert doc.ai_summary_status == "failed"
    assert "Malformed JSON" in doc.ai_summary["error"]
