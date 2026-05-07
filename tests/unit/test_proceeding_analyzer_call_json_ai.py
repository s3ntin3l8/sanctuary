"""Pin: proceeding_analyzer must use the shared `call_json_ai` helper.

Why this matters:
- `call_llm` doesn't reload provider config from the DB → boot-time provider
  is sticky; user model swaps at /settings are silently ignored.
- Manual `json.loads` after `strip()` + fence-removal duplicates and diverges
  from `parse_json_response` in `_json.py`. Truncated LLM responses raise
  unhandled `JSONDecodeError`.
"""

from unittest.mock import patch

import pytest

from app.models.database import Document, Proceeding
from app.models.enums import ProceedingCourtLevel, ProceedingStatus


@pytest.fixture
def proc_doc(db_session, sample_case):
    proc = Proceeding(
        case_id=sample_case.id,
        court_name="Unknown Court",
        court_level=ProceedingCourtLevel.AG,
        status=ProceedingStatus.ACTIVE,
    )
    db_session.add(proc)
    db_session.commit()
    doc = Document(
        title="Test",
        content="Long enough content for analysis. " * 5,
        case_id=sample_case.id,
        proceeding_id=proc.id,
    )
    db_session.add(doc)
    db_session.commit()
    return doc, proc


@pytest.mark.unit
def test_uses_call_json_ai_not_call_llm(db_session, proc_doc):
    """The analyzer must dispatch through `call_json_ai`, which performs a DB
    reload of the AI provider config. `call_llm` would skip that reload."""
    from app.services.intelligence import proceeding_analyzer

    doc, _proc = proc_doc

    with patch.object(proceeding_analyzer, "call_json_ai") as mock_call:
        from app.services.intelligence.schemas import ProceedingExtraction

        mock_call.return_value = ProceedingExtraction.model_validate(
            {"is_court_document": False}
        )
        proceeding_analyzer.analyze_and_update_proceeding(doc, "test-model", db_session)

        assert mock_call.called, (
            "Expected call_json_ai to be used (gives DB-aware provider reload)"
        )
        kwargs = mock_call.call_args.kwargs
        assert "system_prompt" in kwargs
        assert "user_prompt" in kwargs
        assert kwargs.get("db") is db_session
        # call_json_ai must be passed the schema so structured output kicks in
        assert kwargs.get("schema") is ProceedingExtraction, (
            "Expected schema=ProceedingExtraction for grammar-constrained output"
        )
        # debug_label scoped to the doc so AI debug logs route correctly
        label = kwargs.get("debug_label", "")
        assert label.startswith("doc_") and str(doc.id) in label, (
            f"debug_label should be scoped to the doc, got: {label!r}"
        )


@pytest.mark.unit
def test_returns_dict_directly_no_manual_parsing(db_session, proc_doc):
    """call_json_ai returns a Pydantic model — caller materializes it via model_dump."""
    from app.services.intelligence import proceeding_analyzer
    from app.services.intelligence.schemas import ProceedingExtraction

    doc, proc = proc_doc

    with patch.object(proceeding_analyzer, "call_json_ai") as mock_call:
        mock_call.return_value = ProceedingExtraction.model_validate(
            {
                "is_court_document": True,
                "court_level": "ag",
                "court_name": "Amtsgericht Hamburg",
                "az_court": "003 F 426/25",
                "subject_matter": "Custody",
            }
        )
        result = proceeding_analyzer.analyze_and_update_proceeding(
            doc, "test-model", db_session
        )

        assert result is None  # success path
        db_session.refresh(proc)
        assert proc.court_name == "Amtsgericht Hamburg"
        assert proc.az_court == "003 F 426/25"
