"""Pin: DocumentRelationship enforces uniqueness on (from, to, type).

Without this, AI re-runs (e.g. after triage_service._reset_and_reenrich) insert
duplicate edges every time. The graph renderer has no dedup, so the same
arrow renders multiple times in the case correspondence view.

The fix has two layers:
1. UniqueConstraint on the table (DB-level enforcement)
2. link() repository method becomes upsert-style (no IntegrityError on retry)
"""

import pytest

from app.models.database import Document, DocumentRelationship, IngestBatch
from app.models.enums import IngestBatchSourceType, RelationshipType
from app.repositories.document_relationship import DocumentRelationshipRepository


@pytest.fixture
def two_docs(db_session, sample_case):
    batch = IngestBatch(source_type=IngestBatchSourceType.MANUAL)
    db_session.add(batch)
    db_session.commit()
    a = Document(
        title="A", content="x", ingest_batch_id=batch.id, case_id=sample_case.id
    )
    b = Document(
        title="B", content="y", ingest_batch_id=batch.id, case_id=sample_case.id
    )
    db_session.add_all([a, b])
    db_session.commit()
    return a, b


@pytest.mark.unit
def test_link_is_idempotent(db_session, two_docs):
    """Calling link() twice with identical args must result in exactly one row."""
    a, b = two_docs
    repo = DocumentRelationshipRepository(db_session)

    repo.link(a.id, b.id, RelationshipType.REPLIES_TO)
    repo.link(a.id, b.id, RelationshipType.REPLIES_TO)

    rows = (
        db_session.query(DocumentRelationship)
        .filter(
            DocumentRelationship.from_document_id == a.id,
            DocumentRelationship.to_document_id == b.id,
            DocumentRelationship.relationship_type == RelationshipType.REPLIES_TO,
        )
        .all()
    )
    assert len(rows) == 1, (
        f"Expected exactly one edge after two link() calls, got {len(rows)}"
    )


@pytest.mark.unit
def test_different_relationship_types_coexist(db_session, two_docs):
    """The unique constraint scopes (from, to, type) — not (from, to)."""
    a, b = two_docs
    repo = DocumentRelationshipRepository(db_session)

    repo.link(a.id, b.id, RelationshipType.REPLIES_TO)
    repo.link(a.id, b.id, RelationshipType.REFERENCES)

    count = (
        db_session.query(DocumentRelationship)
        .filter(
            DocumentRelationship.from_document_id == a.id,
            DocumentRelationship.to_document_id == b.id,
        )
        .count()
    )
    assert count == 2, f"Expected 2 edges of different types, got {count}"


@pytest.mark.unit
def test_link_returns_existing_row_on_duplicate(db_session, two_docs):
    """The second link() call should return the same DB row, not a new one."""
    a, b = two_docs
    repo = DocumentRelationshipRepository(db_session)

    first = repo.link(a.id, b.id, RelationshipType.REPLIES_TO)
    second = repo.link(a.id, b.id, RelationshipType.REPLIES_TO)

    assert first.id == second.id
