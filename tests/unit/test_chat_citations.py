"""Citation references resolve to the passage ids the reader renders."""

import pytest

from app.models.database import Document
from app.services.case_dashboard_service import key_passages_for_template
from app.services.chat.chat_service import _passage_id
from app.services.chat.context_builder import build_document_chat_prompt


def _doc():
    return Document(
        id=1,
        title="Beschluss",
        content="x",
        key_passages=[
            "not a dict",
            {"text": "", "kind": "neutral"},
            {"text": "Die Sorge wird übertragen", "kind": "ruling"},
            {"text": "Frist: 4 Wochen", "kind": "deadline"},
        ],
    )


@pytest.mark.unit
def test_passage_numbers_match_between_prompt_and_resolver(db_session):
    doc = _doc()
    prompt = build_document_chat_prompt(doc, db_session, [], "q")
    assert "[1] Die Sorge wird übertragen" in prompt
    assert "[2] Frist: 4 Wochen" in prompt
    ids = [p["id"] for p in key_passages_for_template(doc.key_passages)]
    assert _passage_id(doc, "1") == ids[0]
    assert _passage_id(doc, "2") == ids[1]
    assert _passage_id(doc, "3") is None
    assert _passage_id(doc, None) is None
