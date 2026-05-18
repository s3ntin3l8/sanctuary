from datetime import UTC, datetime

import pytest
from sqlalchemy import bindparam, text

from app.models.database import (
    ActionItem,
    Document,
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
def test_delete_batch_removes_all_rows(db_session):
    batch, docs = _make_batch_with_docs(db_session, doc_count=2)
    doc_ids = [d.id for d in docs]
    batch_id = batch.id

    # Per-doc dependents
    db_session.add(UserReaction(document_id=docs[0].id, reaction=UserReactionType.TRUE))
    db_session.add(
        DocumentPin(document_id=docs[0].id, passage_id="abc123", note="my pin")
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
    # Vector row
    db_session.execute(
        text(
            "INSERT INTO document_vectors(document_id, embedding) "
            "VALUES (:id, vec_f32(:vec))"
        ),
        {"id": docs[0].id, "vec": "[" + ",".join(["0.0"] * 768) + "]"},
    )
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
    vec_count = db_session.execute(
        text(
            "SELECT count(*) FROM document_vectors WHERE document_id IN :ids"
        ).bindparams(bindparam("ids", expanding=True)),
        {"ids": doc_ids},
    ).scalar()
    assert vec_count == 0


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
def test_delete_batch_in_processing_state_rejected(db_session):
    batch, _ = _make_batch_with_docs(db_session, doc_count=1)
    batch.status = IngestBatchStatus.PROCESSING
    db_session.commit()

    with pytest.raises(ValueError, match="processing"):
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
