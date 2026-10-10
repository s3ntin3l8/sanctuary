from datetime import UTC, datetime

import pytest

from app.config import AI_EMBED_DIM
from app.models.database import (
    ActionItem,
    Document,
    DocumentChunk,
    DocumentPin,
    IngestBatch,
    UserReaction,
)
from app.models.enums import (
    IngestBatchSourceType,
    IngestBatchStatus,
    UserReactionType,
)
from app.services.triage_dismissal import delete_bundle


def _make_batch_with_docs(db_session, doc_count=2, *, raw_source_path=None):
    batch = IngestBatch(
        source_type=IngestBatchSourceType.EMAIL,
        subject="Delete Me",
        raw_source_path=raw_source_path,
    )
    db_session.add(batch)
    db_session.commit()
    db_session.refresh(batch)
    docs = [
        Document(title=f"Doc {i}", ingest_batch_id=batch.id, case_id="_TRIAGE")
        for i in range(doc_count)
    ]
    db_session.add_all(docs)
    db_session.commit()
    for d in docs:
        db_session.refresh(d)
    return batch, docs


@pytest.mark.unit
def test_delete_batch_removes_all_rows(db_session, sample_user):
    batch, docs = _make_batch_with_docs(db_session, doc_count=2)
    doc_ids = [d.id for d in docs]
    batch_id = batch.id

    # Per-doc dependents
    db_session.add(
        UserReaction(
            document_id=docs[0].id,
            reaction=UserReactionType.TRUE,
            user_id=sample_user.id,
        )
    )
    db_session.add(
        DocumentPin(
            document_id=docs[0].id,
            passage_id="abc123",
            note="my pin",
            user_id=sample_user.id,
        )
    )
    # ActionItem on second doc
    db_session.add(
        ActionItem(
            case_id="_TRIAGE",
            source_document_id=docs[1].id,
            title="Action",
            due_date=datetime.now(UTC),
        )
    )
    # Chunk + vector row
    chunk = DocumentChunk(
        document_id=docs[0].id,
        chunk_index=0,
        text="chunk text",
        embedding=[0.0] * AI_EMBED_DIM,
    )
    db_session.add(chunk)
    db_session.commit()

    assert delete_bundle(db_session, batch_id=batch_id) is True

    assert db_session.get(IngestBatch, batch_id) is None
    for did in doc_ids:
        assert db_session.get(Document, did) is None
    assert (
        db_session.query(UserReaction)
        .filter(UserReaction.document_id.in_(doc_ids))
        .count()
        == 0
    )
    assert (
        db_session.query(DocumentPin)
        .filter(DocumentPin.document_id.in_(doc_ids))
        .count()
        == 0
    )
    assert (
        db_session.query(ActionItem)
        .filter(ActionItem.source_document_id.in_(doc_ids))
        .count()
        == 0
    )
    assert (
        db_session.query(DocumentChunk)
        .filter(DocumentChunk.document_id.in_(doc_ids))
        .count()
        == 0
    )


@pytest.mark.unit
def test_delete_batch_removes_raw_source_file(db_session, tmp_path):
    raw_file = tmp_path / "source.eml"
    raw_file.write_text("From: someone")

    batch, _ = _make_batch_with_docs(
        db_session, doc_count=1, raw_source_path=str(raw_file)
    )

    assert delete_bundle(db_session, batch_id=batch.id) is True
    assert not raw_file.exists()


@pytest.mark.unit
def test_delete_batch_with_missing_raw_source_file(db_session, tmp_path):
    missing = tmp_path / "never_existed.eml"
    batch, _ = _make_batch_with_docs(
        db_session, doc_count=1, raw_source_path=str(missing)
    )
    # Should not raise even though the file doesn't exist
    assert delete_bundle(db_session, batch_id=batch.id) is True


@pytest.mark.unit
def test_delete_batch_in_processing_state_with_no_in_flight_stage_allowed(db_session):
    """PROCESSING is the batch's normal resting state (only an explicit
    "Confirm bundle" ever advances it) — a PROCESSING batch whose pipeline
    work has actually finished (no RUNNING/RETRYING stage) must be
    deletable, or almost every live batch would be permanently stuck."""
    batch, _ = _make_batch_with_docs(db_session, doc_count=1)
    batch.status = IngestBatchStatus.PROCESSING
    db_session.commit()
    batch_id = batch.id

    assert delete_bundle(db_session, batch_id=batch_id) is True
    assert db_session.get(IngestBatch, batch_id) is None


@pytest.mark.unit
def test_delete_batch_with_in_flight_stage_rejected(db_session):
    """The real "unsafe to delete out from under a worker" condition: some
    document's stage is currently RUNNING or RETRYING."""
    from app.models.database import DocumentPipelineStage

    batch, docs = _make_batch_with_docs(db_session, doc_count=1)
    batch.status = IngestBatchStatus.PROCESSING
    db_session.add(
        DocumentPipelineStage(document_id=docs[0].id, stage="extract", status="running")
    )
    db_session.commit()

    with pytest.raises(ValueError, match="actively processing"):
        delete_bundle(db_session, batch_id=batch.id)
    # Batch is still there
    assert db_session.get(IngestBatch, batch.id) is not None


@pytest.mark.unit
def test_delete_batch_in_awaiting_slicing_state_rejected(db_session):
    batch, _ = _make_batch_with_docs(db_session, doc_count=1)
    batch.status = IngestBatchStatus.AWAITING_SLICING
    db_session.commit()

    with pytest.raises(ValueError, match="awaiting_slicing"):
        delete_bundle(db_session, batch_id=batch.id)


@pytest.mark.unit
def test_delete_empty_batch_drops_row(db_session):
    batch = IngestBatch(source_type=IngestBatchSourceType.EMAIL, subject="Empty bundle")
    db_session.add(batch)
    db_session.commit()
    db_session.refresh(batch)
    batch_id = batch.id

    assert delete_bundle(db_session, batch_id=batch_id) is True
    assert db_session.get(IngestBatch, batch_id) is None


@pytest.mark.unit
def test_delete_loose_doc_via_doc_id(db_session):
    doc = Document(title="Synthetic", case_id="_TRIAGE")
    db_session.add(doc)
    db_session.commit()
    db_session.refresh(doc)
    doc_id = doc.id

    assert delete_bundle(db_session, doc_id=doc_id) is True
    assert db_session.get(Document, doc_id) is None


@pytest.mark.unit
def test_delete_unknown_batch_returns_false(db_session):
    assert delete_bundle(db_session, batch_id=999_999) is False


@pytest.mark.unit
def test_delete_unknown_doc_returns_false(db_session):
    assert delete_bundle(db_session, doc_id=999_999) is False


@pytest.mark.unit
def test_delete_doc_with_in_flight_stage_rejected(db_session):
    from app.models.database import DocumentPipelineStage

    _, docs = _make_batch_with_docs(db_session, doc_count=2)
    db_session.add(
        DocumentPipelineStage(document_id=docs[0].id, stage="extract", status="running")
    )
    db_session.commit()

    with pytest.raises(ValueError, match="actively processing"):
        delete_bundle(db_session, doc_id=docs[0].id)
    assert db_session.get(Document, docs[0].id) is not None
    # A sibling that is quiet stays deletable.
    assert delete_bundle(db_session, doc_id=docs[1].id) is True


@pytest.mark.unit
def test_delete_doc_hard_deletes_its_action_items(db_session):
    _, docs = _make_batch_with_docs(db_session, doc_count=2)
    db_session.add(
        ActionItem(
            case_id="_TRIAGE",
            source_document_id=docs[0].id,
            title="Action",
            due_date=datetime.now(UTC),
        )
    )
    db_session.commit()

    assert delete_bundle(db_session, doc_id=docs[0].id) is True
    assert (
        db_session.query(ActionItem).filter(ActionItem.title == "Action").count() == 0
    )


@pytest.mark.unit
def test_delete_cover_letter_keeps_enclosures(db_session):
    _, docs = _make_batch_with_docs(db_session, doc_count=2)
    docs[1].parent_id = docs[0].id
    db_session.commit()
    child_id = docs[1].id

    assert delete_bundle(db_session, doc_id=docs[0].id) is True
    child = db_session.get(Document, child_id)
    assert child is not None
    assert child.parent_id is None


@pytest.mark.unit
def test_delete_last_doc_removes_batch_and_raw_source(db_session, tmp_path):
    raw_file = tmp_path / "source.eml"
    raw_file.write_text("From: someone")
    batch, docs = _make_batch_with_docs(
        db_session, doc_count=2, raw_source_path=str(raw_file)
    )
    batch_id = batch.id

    assert delete_bundle(db_session, doc_id=docs[0].id) is True
    assert raw_file.exists()
    assert db_session.get(IngestBatch, batch_id) is not None

    assert delete_bundle(db_session, doc_id=docs[1].id) is True
    assert db_session.get(IngestBatch, batch_id) is None
    assert not raw_file.exists()
