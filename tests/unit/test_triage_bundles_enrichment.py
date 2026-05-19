"""Unit test for the batched enrich_bundles path in triage_bundles.

enrich_bundles fans out one query per relation type (BatchSubGroup,
ActionItem, DocumentRelationship, Case) keyed by the union of all bundles'
inputs, then redistributes per bundle in Python. This test exercises the
redistribution: items, proof edges, and case metadata must end up on the
correct bundle and only that bundle.
"""

from datetime import UTC, datetime

import pytest

from app.models.database import (
    ActionItem,
    Case,
    Document,
    DocumentRelationship,
    IngestBatch,
)
from app.models.enums import (
    ActionItemType,
    CaseStatus,
    IngestBatchSourceType,
    Jurisdiction,
    OriginatorType,
    RelationshipConfidence,
    RelationshipType,
)


def _doc(
    db_session, *, title: str, batch_id: int, case_id: str = "_TRIAGE"
) -> Document:
    doc = Document(
        title=title,
        content="content",
        case_id=case_id,
        ingest_batch_id=batch_id,
        originator_type=OriginatorType.COURT,
        sender="sender@example.com",
        ingest_date=datetime(2026, 4, 1, tzinfo=UTC),
        issued_date=datetime(2026, 4, 1, tzinfo=UTC),
        received_date=datetime(2026, 4, 1, tzinfo=UTC),
    )
    db_session.add(doc)
    db_session.flush()
    return doc


@pytest.mark.unit
def test_enrich_bundles_distributes_action_items_proof_edges_and_case_metadata(
    db_session,
):
    from app.services.triage_bundles import BundleView, enrich_bundles

    # Two batches, three docs each.
    batch_a = IngestBatch(source_type=IngestBatchSourceType.MANUAL, case_id=None)
    batch_b = IngestBatch(source_type=IngestBatchSourceType.MANUAL, case_id=None)
    db_session.add_all([batch_a, batch_b])
    db_session.flush()

    a1, a2, a3 = (
        _doc(db_session, title="A1 Cover", batch_id=batch_a.id),
        _doc(db_session, title="A2", batch_id=batch_a.id),
        _doc(db_session, title="A3", batch_id=batch_a.id),
    )
    b1, b2, b3 = (
        _doc(db_session, title="B1 Cover", batch_id=batch_b.id),
        _doc(db_session, title="B2", batch_id=batch_b.id),
        _doc(db_session, title="B3", batch_id=batch_b.id),
    )

    # Suggested cases (one draft, one non-draft).
    draft_case = Case(
        id="DRAFT-001",
        title="Some Draft Title",
        status=CaseStatus.INTAKE,
        jurisdiction=Jurisdiction.DE,
        is_draft=True,
    )
    ratified_case = Case(
        id="RATIFIED-001",
        title="Ratified Case Title",
        status=CaseStatus.INTAKE,
        jurisdiction=Jurisdiction.DE,
        is_draft=False,
    )
    db_session.add_all([draft_case, ratified_case])
    db_session.flush()

    # Action items: 2 on a2 (different due dates), 1 on b3, 0 on the others.
    db_session.add_all(
        [
            ActionItem(
                case_id="_TRIAGE",
                source_document_id=a2.id,
                title="A2 deadline",
                due_date=datetime(2026, 6, 1),
                action_type=ActionItemType.DEADLINE,
            ),
            ActionItem(
                case_id="_TRIAGE",
                source_document_id=a2.id,
                title="A2 hearing",
                due_date=datetime(2026, 5, 1),
                action_type=ActionItemType.COURT_DATE,
            ),
            ActionItem(
                case_id="_TRIAGE",
                source_document_id=b3.id,
                title="B3 deadline",
                due_date=datetime(2026, 7, 1),
                action_type=ActionItemType.DEADLINE,
            ),
        ]
    )

    # Proof edge a1 → a3 (so a3 should land in bundle A's proof_doc_ids).
    db_session.add(
        DocumentRelationship(
            from_document_id=a1.id,
            to_document_id=a3.id,
            relationship_type=RelationshipType.ATTACHES_AS_PROOF,
            confidence=RelationshipConfidence.USER_CONFIRMED,
        )
    )
    db_session.commit()

    # Build BundleViews directly. A.suggested = draft, B.suggested = ratified.
    bv_a = BundleView(
        key=f"batch-{batch_a.id}",
        batch_id=batch_a.id,
        source_type=IngestBatchSourceType.MANUAL,
        subject="Batch A",
        sender_email=None,
        received_at=datetime(2026, 4, 1, tzinfo=UTC),
        suggested_case_id="DRAFT-001",
        documents=[a1, a2, a3],
    )
    bv_b = BundleView(
        key=f"batch-{batch_b.id}",
        batch_id=batch_b.id,
        source_type=IngestBatchSourceType.MANUAL,
        subject="Batch B",
        sender_email=None,
        received_at=datetime(2026, 4, 2, tzinfo=UTC),
        suggested_case_id="RATIFIED-001",
        documents=[b1, b2, b3],
    )

    enrich_bundles(db_session, [bv_a, bv_b])

    # Action items split correctly: A has 2 (sorted by due_date), B has 1.
    assert len(bv_a.action_items) == 2
    assert {ai.title for ai in bv_a.action_items} == {"A2 deadline", "A2 hearing"}
    # The implementation sorts by (due_date is None, due_date) — earliest first.
    assert bv_a.action_items[0].title == "A2 hearing"  # 2026-05-01 < 2026-06-01

    assert len(bv_b.action_items) == 1
    assert bv_b.action_items[0].title == "B3 deadline"

    # Proof edges only reach the bundle whose docs were the to_document_id.
    assert bv_a.proof_doc_ids == {a3.id}
    assert bv_b.proof_doc_ids == set()

    # Case metadata: draft path on A, non-draft path on B.
    # Draft: confirmed_case_id stays None; suggested_case_* populated; is_draft True.
    assert bv_a.suggested_case_exists is True
    assert bv_a.suggested_case_is_draft is True
    assert bv_a.suggested_case_id == "DRAFT-001"
    # Sanitized title may differ from raw — just verify it's resolved.
    assert bv_a.suggested_case_title

    # Non-draft suggested: exists, NOT marked draft.
    assert bv_b.suggested_case_exists is True
    assert bv_b.suggested_case_is_draft is False
    assert bv_b.suggested_case_id == "RATIFIED-001"
