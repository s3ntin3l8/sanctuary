from datetime import datetime

from app.models.database import ActionItem, Claim, ClaimEvidence, Document
from app.models.enums import ActionItemStatus, ClaimEvidenceRole, ClaimStatus
from app.services.chat.context_builder import build_case_chat_prompt


def test_build_case_chat_prompt_includes_actions_and_claims(db_session, sample_case):
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
        case_id=sample_case.id,
        claim_text="Contested fact",
        status=ClaimStatus.CONTESTED,
        source_document_id=source_doc.id,
        first_made_at=datetime.now(),
        last_updated_at=datetime.now(),
    )
    db_session.add_all([action, claim])
    db_session.commit()

    # Also add evidence for the claim to verify counts
    evidence = ClaimEvidence(
        claim_id=claim.id,
        document_id=source_doc.id,
        role=ClaimEvidenceRole.SUPPORTS,
        ingest_date=datetime.now(),
    )
    db_session.add(evidence)
    db_session.commit()

    prompt = build_case_chat_prompt(
        case=sample_case,
        db=db_session,
        history=[],
        user_message="Test query",
        retrieved_hits=[],
    )

    assert "Open Action Items / Deadlines:" in prompt
    assert "30.04.2026: Frist test" in prompt
    assert "Contested or Asserted Claims (Truth Map):" in prompt
    assert "[contested] Contested fact (Evidence: 1 supports, 0 contests)" in prompt
