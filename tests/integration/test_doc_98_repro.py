from unittest.mock import MagicMock, patch

import pytest

from app.models.database import Document
from app.models.enums import DocumentRole
from app.services.intelligence.document_enricher import _call_enricher_sync


@pytest.fixture
def mock_call_json_ai():
    with patch("app.services.intelligence.document_enricher.call_json_ai") as mock:
        yield mock


@pytest.mark.unit
def test_enricher_prompt_relaxed_for_cover_letter(mock_call_json_ai):
    """Verify that doc.role=COVER_LETTER results in a relaxed prompt, not a rigid one."""
    doc = Document(
        id=98,
        role=DocumentRole.COVER_LETTER,
        content="Antrag auf alleiniges Sorgerecht... [long document content]",
        title="Original Title",
    )

    # We don't care about the return value for this test, just the prompt construction
    mock_call_json_ai.return_value = MagicMock()

    _call_enricher_sync(doc)

    # Check the user_prompt passed to call_json_ai
    args, kwargs = mock_call_json_ai.call_args
    user_prompt = kwargs.get("user_prompt")

    assert "cover letter" in user_prompt
    assert "Begleitschreiben" in user_prompt
    # Ensure the old rigid MUST instructions are GONE
    assert "MUST title it as a cover letter" not in user_prompt


@pytest.mark.unit
def test_enricher_applies_substantive_title_for_cover_letter_role():
    """_apply_enrichment must accept a substantive title for COVER_LETTER docs
    while keeping document_type=relay and thread_open=False (Option A: prompt
    scopes AI to keep relay classification)."""
    from app.models.enums import DocumentType, SignificanceTier
    from app.services.intelligence.document_enricher import _apply_enrichment

    doc = Document(
        id=98,
        role=DocumentRole.COVER_LETTER,
        content="Antrag auf alleiniges Sorgerecht...",
        title="Original Title",
    )

    result = {
        "title": "Antrag auf alleiniges Sorgerecht",
        "issued_date": None,
        "significance_tier": "administrative",
        "document_type": "relay",
        "key_passages": [],
        "cost_delta": None,
        "management_summary": {
            "legal_significance": "",
            "required_action": "",
            "financial_impact": "",
        },
        "action_items": [],
    }
    _apply_enrichment(doc, result)

    assert doc.title == "Antrag auf alleiniges Sorgerecht"
    assert doc.document_type == DocumentType.RELAY
    assert doc.significance_tier == SignificanceTier.ADMINISTRATIVE
    assert not doc.thread_open
