"""End-to-end cascade test: email ingest → batch analysis → enrichment → claims."""

from datetime import UTC, datetime
from unittest.mock import patch

import pytest

from app.models.database import (
    ActionItem,
    ClaimEvidence,
    Document,
    IngestBatch,
)
from app.models.enums import (
    DocumentRole,
    IngestBatchSourceType,
    IngestBatchStatus,
    OriginatorType,
)

# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


@pytest.fixture
def email_batch(db_session, sample_case):
    """Two-doc email batch: cover letter + one enclosure (the typical court mail shape)."""
    batch = IngestBatch(
        source_type=IngestBatchSourceType.EMAIL,
        sender_email="court@ag-berlin.de",
        subject="Ladung zum Termin – ADV-001",
        case_id=sample_case.id,
        status=IngestBatchStatus.PENDING,
        received_at=datetime.now(UTC),
    )
    db_session.add(batch)
    db_session.flush()

    cover = Document(
        title="Begleitschreiben.pdf",
        content="Begleitschreiben des Amtsgerichts Berlin. Anlage: Urteil.",
        case_id=sample_case.id,
        ingest_batch_id=batch.id,
        originator_type=OriginatorType.COURT,
    )
    enclosure = Document(
        title="Urteil.pdf",
        content="Das Gericht entscheidet: Klage abgewiesen. Kosten trägt der Kläger.",
        case_id=sample_case.id,
        ingest_batch_id=batch.id,
        originator_type=OriginatorType.COURT,
    )
    db_session.add(cover)
    db_session.add(enclosure)
    db_session.commit()
    db_session.refresh(cover)
    db_session.refresh(enclosure)
    return batch, cover, enclosure


# ---------------------------------------------------------------------------
# Mocked AI responses
# ---------------------------------------------------------------------------


def _mock_batch_result(cover_id: int, enclosure_id: int) -> dict:
    return {
        "cover_letter_doc_id": cover_id,
        "is_cover_letter": True,
        "court_relay": True,
        "attributed_originator": "Amtsgericht Berlin",
        "enclosed_descriptions": [
            {
                "doc_id": enclosure_id,
                "matched_filename": "Urteil.pdf",
                "role": "enclosure",
                "document_type": "court_decision",
                "attributed_originator": "Amtsgericht Berlin",
                "originator_type": "court",
            }
        ],
        "detected_actions": [
            {
                "title": "Urteil prüfen",
                "action_type": "deadline",
                "due_date": "2026-05-01",
                "description": "Berufung prüfen",
            }
        ],
    }


def _mock_enrich_result() -> dict:
    return {
        "significance_tier": "critical",
        "document_type": "court_decision",
        "key_passages": [
            {
                "text": "Klage abgewiesen",
                "rationale": "Outcome of the case",
                "span": [0, 16],
            }
        ],
        "management_summary": {
            "legal_significance": "Court dismissed the claim.",
            "required_action": "Consider appeal by 2026-05-01.",
            "financial_impact": "Cost order against plaintiff.",
        },
        "cost_delta": None,
        "thread_open": False,
    }


def _mock_claim_result(doc_id: int) -> dict:
    return {
        "new_claims": [
            {
                "claim_text": "Das Gericht hat die Klage abgewiesen.",
                "claim_type": "legal",
                "status": "established",
                "excerpt": "Das Gericht entscheidet: Klage abgewiesen.",
            }
        ],
        "evidence_links": [],
    }


# ---------------------------------------------------------------------------
# The cascade test
# ---------------------------------------------------------------------------


@pytest.mark.integration
def test_ingestion_cascade(db_session, email_batch):
    """Walk the full pipeline: batch → enrichment → claims.

    Each AI call is mocked; the test asserts that the pipeline correctly wires
    results across stages.
    """
    batch, cover, enclosure = email_batch
    cover_id = cover.id
    enclosure_id = enclosure.id

    # ── Stage 1: Batch analysis ──────────────────────────────────────────────
    from app.services.intelligence.batch_analyzer import analyze

    with (
        patch(
            "app.services.intelligence.batch_analyzer.SessionLocal",
            return_value=db_session,
        ),
        patch.object(db_session, "close"),
        patch(
            "app.services.intelligence.batch_analyzer._call_batch_analyzer_sync",
            return_value=_mock_batch_result(cover_id, enclosure_id),
        ),
    ):
        analyze(batch.id)

    db_session.refresh(cover)
    db_session.refresh(enclosure)

    assert cover.role == DocumentRole.COVER_LETTER
    # court_relay is owned by METADATA — batch analyzer must not overwrite it
    # (the AI's legacy court_relay key is intentionally ignored).
    assert cover.court_relay is False
    assert cover.attributed_originator == "Amtsgericht Berlin"
    assert enclosure.role == DocumentRole.ENCLOSURE
    assert enclosure.attributed_originator == "Amtsgericht Berlin"

    action_items = (
        db_session.query(ActionItem).filter(ActionItem.case_id == cover.case_id).all()
    )
    assert len(action_items) >= 1
    assert any(
        "Urteil" in (ai.title or "") or "prüfen" in (ai.title or "")
        for ai in action_items
    )

    # ── Stage 2: Document enrichment ─────────────────────────────────────────
    from app.services.intelligence.document_enricher import enrich

    enrich_result = _mock_enrich_result()
    with (
        patch(
            "app.services.intelligence.document_enricher.SessionLocal",
            return_value=db_session,
        ),
        patch.object(db_session, "close"),
        patch(
            "app.services.intelligence.document_enricher._call_enricher_sync",
            return_value=enrich_result,
        ),
    ):
        enrich(enclosure_id)

    db_session.refresh(enclosure)

    assert enclosure.significance_tier is not None
    assert enclosure.significance_tier.value == "critical"
    assert enclosure.ai_summary is not None
    bullets = enclosure.ai_summary
    assert "legal_significance" in bullets
    assert "required_action" in bullets
    assert "financial_impact" in bullets

    # ── Stage 3: Claim extraction ────────────────────────────────────────────
    from app.services.intelligence.claim_extractor import extract

    with (
        patch(
            "app.services.intelligence.claim_extractor.SessionLocal",
            return_value=db_session,
        ),
        patch.object(db_session, "close"),
        patch(
            "app.services.intelligence.claim_extractor._call_claim_extractor_sync",
            return_value=_mock_claim_result(enclosure_id),
        ),
    ):
        extract(enclosure_id)

    from app.repositories.claim import ClaimRepository

    claims = list(ClaimRepository(db_session).claims_for_case(enclosure.case_id))
    assert len(claims) >= 1

    evidence = (
        db_session.query(ClaimEvidence)
        .filter(ClaimEvidence.claim_id == claims[0].id)
        .all()
    )
    assert len(evidence) >= 1
    assert evidence[0].excerpt is not None
    assert len(evidence[0].excerpt) > 0
