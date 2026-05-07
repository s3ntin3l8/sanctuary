"""Unit tests for TriageService confirm_bundle / BundleView correctness."""

from datetime import UTC, datetime

import pytest

from app.models.database import ActionItem, Document, DocumentRelationship, IngestBatch
from app.models.enums import (
    ActionItemType,
    CaseStatus,
    DocumentRole,
    IngestBatchSourceType,
    Jurisdiction,
    OriginatorType,
    RelationshipConfidence,
    RelationshipType,
)
from app.services.ingestion.service import compute_review_reasons

# ---------------------------------------------------------------------------
# compute_review_reasons — missing_parent behaviour
# ---------------------------------------------------------------------------


@pytest.mark.unit
def test_compute_review_reasons_missing_parent_only_for_enclosure(db_session):
    """Cover letters (non-ENCLOSURE) should NOT get missing_parent reason."""

    # `_TRIAGE` is pre-seeded by the conftest cleanup_per_test fixture.

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
    # pending_confirmation is now mandatory if not explicitly confirmed
    assert "pending_confirmation" in reasons
    assert "missing_case_id" in reasons
    assert "missing_parent" not in reasons


@pytest.mark.unit
def test_compute_review_reasons_enclosure_without_parent_flagged(db_session):
    """ENCLOSURE docs without a parent_id should get missing_parent reason."""

    # `_TRIAGE` is pre-seeded by the conftest cleanup_per_test fixture.

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
    assert "pending_confirmation" in reasons
    assert "missing_parent" in reasons


@pytest.mark.unit
def test_compute_review_reasons_low_confidence(db_session):
    """Low extraction confidence should trigger low_confidence reason."""
    from app.models.database import Document

    doc = Document(
        title="Test",
        case_id="ADV-123",
        originator_type=OriginatorType.OWN,
        sender="me@example.com",
        received_date=datetime.now(UTC),
        extraction_confidence={"sender": "low"},
    )
    reasons = compute_review_reasons(doc)
    assert "pending_confirmation" in reasons
    assert "low_confidence" in reasons


@pytest.mark.unit
def test_compute_review_reasons_unresolved_relationship(db_session, sample_case):
    """Unconfirmed AI relationships should trigger unresolved_relationship reason."""
    from app.models.database import Document, DocumentRelationship
    from app.models.enums import RelationshipConfidence

    doc = Document(
        title="Test",
        case_id=sample_case.id,
        originator_type=OriginatorType.OWN,
        sender="me@example.com",
        received_date=datetime.now(UTC),
    )
    other = Document(
        title="Target",
        case_id=sample_case.id,
        originator_type=OriginatorType.OWN,
    )
    db_session.add_all([doc, other])
    db_session.flush()

    rel = DocumentRelationship(
        from_document_id=doc.id,
        to_document_id=other.id,
        relationship_type=RelationshipType.REPLIES_TO,
        confidence=RelationshipConfidence.AI_DETECTED,
    )
    db_session.add(rel)
    db_session.commit()
    db_session.refresh(doc)

    reasons = compute_review_reasons(doc)
    assert "pending_confirmation" in reasons
    assert "unresolved_relationship" in reasons


@pytest.mark.unit
def test_compute_review_reasons_contradiction(db_session):
    """AI contradiction flag in meta should trigger contradiction_detected reason."""
    from app.models.database import Document

    doc = Document(
        title="Test",
        case_id="ADV-123",
        originator_type=OriginatorType.OWN,
        sender="me@example.com",
        received_date=datetime.now(UTC),
        meta={"ai_contradiction": True},
    )
    reasons = compute_review_reasons(doc)
    assert "pending_confirmation" in reasons
    assert "contradiction_detected" in reasons


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
            issued_date=spec.get("issued_date", datetime(2026, 1, 1, tzinfo=UTC)),
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

    # `_TRIAGE` is pre-seeded by the conftest cleanup_per_test fixture.
    triage_case = db_session.query(Case).filter_by(id="_TRIAGE").one()
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
    svc.confirm_bundle(batch.id, target_case.id, finalize=True)

    db_session.refresh(docs[0])
    assert docs[0].needs_review is False
    assert docs[0].case_id == target_case.id


@pytest.mark.unit
def test_confirm_bundle_keeps_doc_in_triage_when_sender_missing(db_session):
    """Doc missing sender should stay in triage even after case is assigned."""
    from app.models.database import Case

    # `_TRIAGE` is pre-seeded by the conftest cleanup_per_test fixture.
    triage_case = db_session.query(Case).filter_by(id="_TRIAGE").one()
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

    # `_TRIAGE` is pre-seeded by the conftest cleanup_per_test fixture.
    triage_case = db_session.query(Case).filter_by(id="_TRIAGE").one()
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


# ---------------------------------------------------------------------------
# BundleView — proof_doc_ids population
# ---------------------------------------------------------------------------


@pytest.mark.unit
def test_bundle_view_proof_doc_ids_populated(db_session):
    """proof_doc_ids should contain the to_document_id of ATTACHES_AS_PROOF edges."""
    from app.models.database import Case

    # `_TRIAGE` is pre-seeded by the conftest cleanup_per_test fixture.
    triage_case = db_session.query(Case).filter_by(id="_TRIAGE").one()

    batch, docs = _make_batch_with_docs(
        db_session,
        triage_case,
        triage_case,
        [
            {
                "title": "Cover Letter",
                "role": DocumentRole.COVER_LETTER,
                "reasons": ["missing_case_id"],
            },
            {
                "title": "Exhibit A",
                "role": DocumentRole.ENCLOSURE,
                "reasons": ["missing_case_id"],
            },
        ],
    )
    cover, exhibit = docs

    rel = DocumentRelationship(
        from_document_id=cover.id,
        to_document_id=exhibit.id,
        relationship_type=RelationshipType.ATTACHES_AS_PROOF,
        confidence=RelationshipConfidence.USER_CONFIRMED,
    )
    db_session.add(rel)
    db_session.commit()

    from app.services.triage_service import TriageService

    svc = TriageService(db_session)
    bundles = svc.get_triage_bundles()

    assert bundles, "expected at least one bundle"
    bundle = next((b for b in bundles if b.batch_id == batch.id), None)
    assert bundle is not None
    assert exhibit.id in bundle.proof_doc_ids
    assert cover.id not in bundle.proof_doc_ids


# ---------------------------------------------------------------------------
# BundleView — proceeding chip data flow
# ---------------------------------------------------------------------------


@pytest.mark.unit
def test_bundle_view_proceeding_populated(db_session):
    """bundle.proceeding should reflect the batch's linked Proceeding."""
    from app.models.database import Case, Proceeding

    # `_TRIAGE` is pre-seeded by the conftest cleanup_per_test fixture.
    triage_case = db_session.query(Case).filter_by(id="_TRIAGE").one()
    target_case = Case(
        id="ADV-PROC-T",
        title="Proceeding Test Case",
        status=CaseStatus.INTAKE,
        jurisdiction=Jurisdiction.DE,
    )
    db_session.add_all([triage_case, target_case])
    db_session.commit()

    proceeding = Proceeding(
        case_id=target_case.id,
        court_name="AG Hamburg",
        court_level="AG",
        az_court="003 F 99/25",
    )
    db_session.add(proceeding)
    db_session.commit()
    db_session.refresh(proceeding)

    batch = IngestBatch(
        source_type=IngestBatchSourceType.MANUAL,
        case_id=target_case.id,
        proceeding_id=proceeding.id,
        received_at=datetime(2026, 4, 1, tzinfo=UTC),
    )
    db_session.add(batch)
    db_session.flush()

    doc = Document(
        title="Court Notice",
        content="content",
        case_id="_TRIAGE",
        role=DocumentRole.COVER_LETTER,
        originator_type=OriginatorType.COURT,
        sender="court@ag-hamburg.de",
        received_date=datetime(2026, 4, 1, tzinfo=UTC),
        ingest_batch_id=batch.id,
        needs_review=True,
        review_reasons=["missing_case_id"],
    )
    db_session.add(doc)
    db_session.commit()

    from app.services.triage_service import TriageService

    svc = TriageService(db_session)
    bundles = svc.get_triage_bundles()

    bundle = next((b for b in bundles if b.batch_id == batch.id), None)
    assert bundle is not None
    assert bundle.proceeding is not None
    assert bundle.proceeding.court_name == "AG Hamburg"


# ---------------------------------------------------------------------------
# confirm_document — finalize-always behaviour
# ---------------------------------------------------------------------------


@pytest.mark.unit
def test_confirm_document_clears_needs_review_when_all_fields_present(db_session):
    """confirm_document with finalize=True should clear needs_review when all required fields are populated."""
    from app.models.database import Case

    # `_TRIAGE` is pre-seeded by the conftest cleanup_per_test fixture.
    triage_case = db_session.query(Case).filter_by(id="_TRIAGE").one()
    target_case = Case(
        id="ADV-FIN-T",
        title="Finalize Test Case",
        status=CaseStatus.INTAKE,
        jurisdiction=Jurisdiction.DE,
    )
    db_session.add_all([triage_case, target_case])
    db_session.commit()

    doc = Document(
        title="Test Document",
        content="content",
        case_id="_TRIAGE",
        role=DocumentRole.COVER_LETTER,
        originator_type=None,
        sender=None,
        issued_date=None,
        received_date=None,
        needs_review=True,
        review_reasons=[
            "missing_case_id",
            "missing_originator",
            "missing_sender",
            "missing_issued_date",
        ],
    )
    db_session.add(doc)
    db_session.commit()
    db_session.refresh(doc)

    from app.services.triage_service import TriageService

    svc = TriageService(db_session)
    updated = svc.confirm_document(
        doc.id,
        title="Test Document",
        case_id=target_case.id,
        originator_type=OriginatorType.COURT,
        sender="court@example.com",
        issued_date=datetime(2026, 4, 1, tzinfo=UTC),
        received_date=datetime(2026, 4, 1, tzinfo=UTC),
        finalize=True,
    )

    assert updated is not None
    assert updated.needs_review is False
    assert (
        updated.review_reasons == []
        or updated.review_reasons is None
        or len(updated.review_reasons) == 0
    )


@pytest.mark.unit
def test_confirm_bundle_removes_bundle_from_triage_feed_even_with_review_flags(
    db_session,
):
    """After "Confirm bundle" (finalize=True), the bundle must drop out of
    get_triage_bundles() even if individual docs still carry needs_review=True
    (e.g. low_confidence on extracted metadata, unresolved_relationship). The
    review flags remain on the doc for case-view UI; they no longer drag the
    bundle back into the triage feed.

    Regression: ib-0008 (1 doc with low_confidence) and ib-0009 (3 docs with
    low_confidence + unresolved_relationship) reproduced the old behavior —
    user clicked Confirm, batch.status went to COMPLETED, but the bundles
    stayed visible in the triage UI.
    """
    from app.models.database import Case
    from app.services.triage_service import TriageService

    triage_case = db_session.query(Case).filter_by(id="_TRIAGE").one()
    target_case = Case(
        id="ADV-090-Z",
        title="Target Case",
        status=CaseStatus.INTAKE,
        jurisdiction=Jurisdiction.DE,
    )
    db_session.add_all([triage_case, target_case])
    db_session.commit()

    # Build a bundle whose docs will retain needs_review=True after confirm
    # (low_confidence is non-clearable by confirm_bundle).
    batch, docs = _make_batch_with_docs(
        db_session,
        triage_case,
        target_case,
        [
            {
                "title": "Cover",
                "role": DocumentRole.COVER_LETTER,
                "sender": "court@example.com",
                "reasons": ["low_confidence"],
            },
            {
                "title": "Enclosure",
                "role": DocumentRole.ENCLOSURE,
                "sender": "court@example.com",
                "reasons": ["low_confidence"],
            },
        ],
    )

    # Manually pre-set extraction_confidence so compute_review_reasons keeps
    # "low_confidence" after the cascade.
    for d in docs:
        d.extraction_confidence = {"sender": "low"}
    db_session.commit()

    svc = TriageService(db_session)

    # Sanity: bundle is in the feed before confirm.
    pre = svc.get_triage_bundles()
    assert any(b.batch_id == batch.id for b in pre), (
        "bundle must be visible in triage before confirm"
    )

    # Act: confirm the bundle with finalize=True.
    svc.confirm_bundle(batch.id, target_case.id, finalize=True)

    # Refresh and verify: docs still carry needs_review (low_confidence
    # survives), but the bundle no longer appears in the triage feed.
    for d in docs:
        db_session.refresh(d)
        assert d.needs_review is True, (
            "low_confidence must keep needs_review=True for case-view consumers"
        )

    post = svc.get_triage_bundles()
    assert not any(b.batch_id == batch.id for b in post), (
        "confirmed bundle must NOT appear in triage feed even with needs_review docs"
    )


@pytest.mark.unit
def test_open_batch_with_needs_review_still_appears_in_feed(db_session):
    """The fix above must NOT regress the open-bundle case: a doc with
    needs_review in a bundle whose batch is still PENDING must remain
    visible in the triage feed."""
    from app.models.database import Case
    from app.services.triage_service import TriageService

    triage_case = db_session.query(Case).filter_by(id="_TRIAGE").one()
    target_case = Case(
        id="ADV-091-Z",
        title="Target",
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
                "title": "Cover",
                "role": DocumentRole.COVER_LETTER,
                "sender": "court@example.com",
                "reasons": ["low_confidence"],
            }
        ],
    )
    # Batch is PENDING by default in the helper (no status overrides). Sanity:
    from app.models.enums import IngestBatchStatus

    assert batch.status == IngestBatchStatus.PENDING

    svc = TriageService(db_session)
    bundles = svc.get_triage_bundles()
    assert any(b.batch_id == batch.id for b in bundles)


@pytest.mark.unit
def test_loose_doc_with_needs_review_still_appears_in_feed(db_session):
    """Loose docs (no batch) with needs_review must still surface in triage —
    they have no batch.status to gate on, so needs_review is the only signal."""
    from app.models.database import Case, Document
    from app.services.triage_service import TriageService

    triage_case = db_session.query(Case).filter_by(id="_TRIAGE").one()
    target_case = Case(
        id="ADV-092-Z",
        title="Target",
        status=CaseStatus.INTAKE,
        jurisdiction=Jurisdiction.DE,
    )
    db_session.add_all([triage_case, target_case])
    db_session.commit()

    loose = Document(
        title="Loose doc",
        content="content",
        case_id=target_case.id,  # already has a real case
        ingest_batch_id=None,  # no batch
        needs_review=True,
        review_reasons=["low_confidence"],
        extraction_confidence={"sender": "low"},
    )
    db_session.add(loose)
    db_session.commit()
    db_session.refresh(loose)

    svc = TriageService(db_session)
    bundles = svc.get_triage_bundles()
    assert any(
        b.batch_id is None and any(d.id == loose.id for d in b.documents)
        for b in bundles
    ), "loose doc with needs_review must remain visible in triage"
