from datetime import datetime

from app.models.database import ActionItem, Claim, ClaimEvidence, Document
from app.models.enums import (
    ActionItemStatus,
    BriefState,
    ClaimEvidenceRole,
    ClaimStatus,
)
from app.services.chat.context_builder import build_case_chat_prompt


def test_build_case_chat_prompt_includes_chronology_and_claims(db_session, sample_case):
    # Source document for the Claim's source_document_id FK.
    source_doc = Document(
        title="Source", content="x", case_id=sample_case.id, needs_review=False
    )
    db_session.add(source_doc)
    db_session.commit()

    action = ActionItem(
        case_id=sample_case.id,
        title="Test Deadline",
        description="Frist test",
        due_date=datetime(2026, 4, 30),
        status=ActionItemStatus.OPEN,
    )
    claim = Claim(
        claim_text="Contested fact",
        status=ClaimStatus.CONTESTED,
        first_made_at=datetime.now(),
        last_updated_at=datetime.now(),
    )
    db_session.add_all([action, claim])
    db_session.flush()

    # ASSERTS row scopes the claim to source_doc's case; SUPPORTS row gives
    # the evidence-count verification something to count.
    db_session.add_all(
        [
            ClaimEvidence(
                claim_id=claim.id,
                document_id=source_doc.id,
                role=ClaimEvidenceRole.ASSERTS,
                ingest_date=datetime.now(),
            ),
            ClaimEvidence(
                claim_id=claim.id,
                document_id=source_doc.id,
                role=ClaimEvidenceRole.SUPPORTS,
                ingest_date=datetime.now(),
            ),
        ]
    )
    db_session.commit()

    prompt = build_case_chat_prompt(
        case=sample_case,
        db=db_session,
        history=[],
        user_message="Test query",
        retrieved_hits=[],
    )

    assert "Case chronology (oldest first):" in prompt
    assert "OVERDUE (open," in prompt
    assert "Test Deadline" in prompt
    assert "[from DOC:None]" not in prompt
    assert "Open Action Items / Deadlines:" not in prompt
    assert "Contested or Asserted Claims (Truth Map):" in prompt
    assert (
        "[contested] Contested fact (Evidence: 1 supports, 0 contests, first made"
        in prompt
    )
    assert f"[DOC:{source_doc.id}]" in prompt


def test_build_case_chat_prompt_shows_brief_while_refreshing(db_session, sample_case):
    sample_case.ai_brief = {"posture": "Kept posture", "pressure_points": []}
    sample_case.brief_state = BriefState.PROCESSING
    db_session.commit()

    prompt = build_case_chat_prompt(
        case=sample_case,
        db=db_session,
        history=[],
        user_message="q",
        retrieved_hits=[],
    )

    assert "Posture: Kept posture" in prompt
