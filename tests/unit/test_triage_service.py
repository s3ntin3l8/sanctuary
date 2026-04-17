"""Unit tests for TriageService confirm_bundle correctness (P0 spec compliance)."""

from datetime import UTC, datetime

import pytest

from app.models.database import ActionItem, Document, IngestBatch
from app.models.enums import (
    ActionItemType,
    CaseStatus,
    DocumentRole,
    IngestBatchSourceType,
    Jurisdiction,
    OriginatorType,
)
from app.services.ingestion.service import compute_review_reasons

# ---------------------------------------------------------------------------
# compute_review_reasons — missing_parent behaviour
# ---------------------------------------------------------------------------


@pytest.mark.unit
def test_compute_review_reasons_missing_parent_only_for_enclosure(db_session):
    """Cover letters (non-ENCLOSURE) should NOT get missing_parent reason."""
    from app.models.database import Case

    case = Case(
        id="_TRIAGE",
        title="Triage Inbox",
        status=CaseStatus.INTAKE,
        jurisdiction=Jurisdiction.DE,
    )
    db_session.add(case)
    db_session.commit()

    cover = Document(
        title="Cover Letter",
        content="content",
        case_id="_TRIAGE",
        role=DocumentRole.COVER_LETTER,
        originator_type=OriginatorType.COURT,
        sender="court@example.com",
        received_date=datetime(2026, 1, 1, tzinfo=UTC),
        parent_id=None,
    )
    db_session.add(cover)
    db_session.commit()
    db_session.refresh(cover)

    reasons = compute_review_reasons(cover)
    # missing_case_id is expected because case_id == '_TRIAGE'
    assert "missing_parent" not in reasons


@pytest.mark.unit
def test_compute_review_reasons_enclosure_without_parent_flagged(db_session):
    """ENCLOSURE docs without a parent_id should get missing_parent reason."""
    from app.models.database import Case

    case = Case(
        id="_TRIAGE",
        title="Triage Inbox",
        status=CaseStatus.INTAKE,
        jurisdiction=Jurisdiction.DE,
    )
    db_session.add(case)
    db_session.commit()

    enclosure = Document(
        title="Annex A",
        content="content",
        case_id="_TRIAGE",
        role=DocumentRole.ENCLOSURE,
        originator_type=OriginatorType.COURT,
        sender="court@example.com",
        received_date=datetime(2026, 1, 1, tzinfo=UTC),
        parent_id=None,
    )
    db_session.add(enclosure)
    db_session.commit()
    db_session.refresh(enclosure)

    reasons = compute_review_reasons(enclosure)
    assert "missing_parent" in reasons


# ---------------------------------------------------------------------------
# confirm_bundle — conditional needs_review clear
# ---------------------------------------------------------------------------


def _make_batch_with_docs(db_session, triage_case, target_case, docs_spec):
    """Helper: create an IngestBatch + list of Documents per spec list."""
    batch = IngestBatch(
        source_type=IngestBatchSourceType.MANUAL,
        case_id=None,
        received_at=datetime(2026, 4, 1, tzinfo=UTC),
    )
    db_session.add(batch)
    db_session.flush()

    docs = []
    for spec in docs_spec:
        doc = Document(
            title=spec.get("title", "Doc"),
            content="content",
            case_id=triage_case.id,
            role=spec.get("role", DocumentRole.COVER_LETTER),
            originator_type=OriginatorType.COURT,
            sender=spec.get("sender", "sender@example.com"),
            received_date=spec.get("received_date", datetime(2026, 1, 1, tzinfo=UTC)),
            ingest_batch_id=batch.id,
            parent_id=spec.get("parent_id"),
            needs_review=True,
            review_reasons=spec.get("reasons", ["missing_case_id"]),
        )
        db_session.add(doc)
        docs.append(doc)
    db_session.commit()
    for doc in docs:
        db_session.refresh(doc)
    return batch, docs


@pytest.mark.unit
def test_confirm_bundle_clears_doc_with_only_missing_case_id(db_session):
    """Doc whose only blocker was missing_case_id should leave triage after cascade."""
    from app.models.database import Case

    triage_case = Case(
        id="_TRIAGE",
        title="Triage Inbox",
        status=CaseStatus.INTAKE,
        jurisdiction=Jurisdiction.DE,
    )
    target_case = Case(
        id="ADV-001-T",
        title="Target Case",
        status=CaseStatus.INTAKE,
        jurisdiction=Jurisdiction.DE,
    )
    db_session.add_all([triage_case, target_case])
    db_session.commit()

    batch, docs = _make_batch_with_docs(
        db_session,
        triage_case,
        target_case,
        [
            {
                "title": "Cover Letter",
                "role": DocumentRole.COVER_LETTER,
                "sender": "court@example.com",
                "received_date": datetime(2026, 1, 1, tzinfo=UTC),
                "reasons": ["missing_case_id"],
            }
        ],
    )

    from app.services.triage_service import TriageService

    svc = TriageService(db_session)
    svc.confirm_bundle(batch.id, target_case.id)

    db_session.refresh(docs[0])
    assert docs[0].needs_review is False
    assert docs[0].case_id == target_case.id


@pytest.mark.unit
def test_confirm_bundle_keeps_doc_in_triage_when_sender_missing(db_session):
    """Doc missing sender should stay in triage even after case is assigned."""
    from app.models.database import Case

    triage_case = Case(
        id="_TRIAGE",
        title="Triage Inbox",
        status=CaseStatus.INTAKE,
        jurisdiction=Jurisdiction.DE,
    )
    target_case = Case(
        id="ADV-002-T",
        title="Target Case",
        status=CaseStatus.INTAKE,
        jurisdiction=Jurisdiction.DE,
    )
    db_session.add_all([triage_case, target_case])
    db_session.commit()

    batch, docs = _make_batch_with_docs(
        db_session,
        triage_case,
        target_case,
        [
            {
                "title": "Missing Sender Doc",
                "role": DocumentRole.COVER_LETTER,
                "sender": None,  # <-- will trigger missing_sender
                "received_date": datetime(2026, 1, 1, tzinfo=UTC),
                "reasons": ["missing_case_id", "missing_sender"],
            }
        ],
    )

    from app.services.triage_service import TriageService

    svc = TriageService(db_session)
    svc.confirm_bundle(batch.id, target_case.id)

    db_session.refresh(docs[0])
    assert docs[0].needs_review is True
    assert "missing_sender" in docs[0].review_reasons


@pytest.mark.unit
def test_confirm_bundle_cascades_case_to_action_items(db_session):
    """ActionItems parked under _TRIAGE linked to bundle docs get their case_id updated."""
    from app.models.database import Case

    triage_case = Case(
        id="_TRIAGE",
        title="Triage Inbox",
        status=CaseStatus.INTAKE,
        jurisdiction=Jurisdiction.DE,
    )
    target_case = Case(
        id="ADV-003-T",
        title="Target Case",
        status=CaseStatus.INTAKE,
        jurisdiction=Jurisdiction.DE,
    )
    db_session.add_all([triage_case, target_case])
    db_session.commit()

    batch, docs = _make_batch_with_docs(
        db_session,
        triage_case,
        target_case,
        [
            {
                "title": "Cover Letter with Deadline",
                "role": DocumentRole.COVER_LETTER,
                "sender": "court@example.com",
                "received_date": datetime(2026, 1, 1, tzinfo=UTC),
            }
        ],
    )
    cover_doc = docs[0]

    # Simulate Phase 4 creating an ActionItem before bundle confirm
    action_item = ActionItem(
        case_id="_TRIAGE",
        source_document_id=cover_doc.id,
        title="Respond by 2026-05-01",
        due_date=datetime(2026, 5, 1, tzinfo=UTC),
        action_type=ActionItemType.DEADLINE,
    )
    db_session.add(action_item)
    db_session.commit()
    db_session.refresh(action_item)

    from app.services.triage_service import TriageService

    svc = TriageService(db_session)
    svc.confirm_bundle(batch.id, target_case.id)

    db_session.refresh(action_item)
    assert action_item.case_id == target_case.id
