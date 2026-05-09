"""Pin: document enricher must include user reactions in its prompt.

CLAUDE.md: "UserReaction — triage reactions stored and recalled by AI during
case brief and document enrichment." Without this, 🚩/✅/🔍/⚖️ signals are
ignored when the LLM picks `significance_tier` and `key_passages`, which is
exactly the wrong direction — flagged docs should bias toward `critical` and
`needs_proof` reactions should drive evidence extraction.
"""

from unittest.mock import patch

import pytest


@pytest.mark.unit
def test_enricher_prompt_includes_user_reactions(db_session):
    from app.models.database import Case, Document, IngestBatch, UserReaction
    from app.models.enums import IngestBatchSourceType, UserReactionType
    from app.services.intelligence import document_enricher

    case = Case(id="ENR-1", title="t")
    db_session.add(case)
    batch = IngestBatch(source_type=IngestBatchSourceType.MANUAL)
    db_session.add(batch)
    db_session.commit()
    doc = Document(
        title="Schreiben vom 12.03",
        content="Die Gegenseite behauptet, der Mandant sei nicht erschienen.",
        ingest_batch_id=batch.id,
        case_id=case.id,
    )
    db_session.add(doc)
    db_session.commit()
    db_session.add(
        UserReaction(
            document_id=doc.id,
            reaction=UserReactionType.LIES,
            notes="Termin war verlegt — Beleg im Postfach",
        )
    )
    db_session.commit()

    from app.services.intelligence.reaction_context import format_reactions_for_document

    formatted = format_reactions_for_document(db_session, doc.id)
    reactions_block = f"\n\n{formatted}" if formatted else ""

    captured = {}

    def fake_call(system_prompt, user_prompt, **kwargs):
        from app.services.intelligence.schemas import DocumentEnrichment

        captured["user_prompt"] = user_prompt
        return DocumentEnrichment.model_validate({})

    with patch.object(document_enricher, "call_json_ai", side_effect=fake_call):
        document_enricher._call_enricher_sync(
            doc, model="", reactions_block=reactions_block
        )

    assert "user_prompt" in captured
    prompt = captured["user_prompt"]
    assert "Lies" in prompt or "🚩" in prompt, (
        f"Expected reaction marker in enricher prompt, got: {prompt[:400]}"
    )
    assert "Termin war verlegt" in prompt, (
        f"Expected reaction note in enricher prompt, got: {prompt[:400]}"
    )


@pytest.mark.unit
def test_enricher_prompt_skips_reactions_block_when_none(db_session):
    """No reactions → no 'User reactions' header in the prompt (clean)."""
    from app.models.database import Case, Document, IngestBatch
    from app.models.enums import IngestBatchSourceType
    from app.services.intelligence import document_enricher

    case = Case(id="ENR-2", title="t")
    db_session.add(case)
    batch = IngestBatch(source_type=IngestBatchSourceType.MANUAL)
    db_session.add(batch)
    db_session.commit()
    doc = Document(
        title="t",
        content="kurzer text",
        ingest_batch_id=batch.id,
        case_id=case.id,
    )
    db_session.add(doc)
    db_session.commit()

    captured = {}

    def fake_call(system_prompt, user_prompt, **kwargs):
        from app.services.intelligence.schemas import DocumentEnrichment

        captured["user_prompt"] = user_prompt
        return DocumentEnrichment.model_validate({})

    with patch.object(document_enricher, "call_json_ai", side_effect=fake_call):
        document_enricher._call_enricher_sync(doc, model="", reactions_block="")

    assert "User reactions on this document" not in captured["user_prompt"]
