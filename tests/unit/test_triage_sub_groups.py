"""Integration tests for BatchSubGroup mutation methods in the triage service."""

import pytest

from app.models.database import BatchSubGroup, Document, IngestBatch
from app.models.enums import DocumentRole, IngestBatchSourceType, IngestBatchStatus
from app.services.triage_subgroups import (
    create_sub_group,
    ensure_sub_groups_initialized,
    rename_sub_group,
    reorder_documents,
    set_cover_letter,
)


def _make_batch(db_session) -> IngestBatch:
    batch = IngestBatch(
        source_type=IngestBatchSourceType.EMAIL,
        status=IngestBatchStatus.PENDING,
    )
    db_session.add(batch)
    db_session.flush()
    return batch


def _make_doc(db_session, batch_id: int, title="Doc", parent_id=None) -> Document:
    doc = Document(
        title=title,
        ingest_batch_id=batch_id,
        parent_id=parent_id,
        case_id="_TRIAGE",
    )
    db_session.add(doc)
    db_session.flush()
    return doc


@pytest.mark.unit
def test_ensure_sub_groups_initialized_idempotent(db_session):
    batch = _make_batch(db_session)
    _make_doc(db_session, batch.id)
    first = ensure_sub_groups_initialized(db_session, batch.id)
    second = ensure_sub_groups_initialized(db_session, batch.id)
    assert len(first) == len(second) == 1


@pytest.mark.unit
def test_ensure_sub_groups_one_per_root(db_session):
    batch = _make_batch(db_session)
    root1 = _make_doc(db_session, batch.id, "Root1")
    _make_doc(db_session, batch.id, "Root2")
    child = _make_doc(db_session, batch.id, "Child", parent_id=root1.id)
    groups = ensure_sub_groups_initialized(db_session, batch.id)
    assert len(groups) == 2
    db_session.refresh(child)
    db_session.refresh(root1)
    assert root1.sub_group_id == child.sub_group_id


@pytest.mark.unit
def test_set_cover_letter_clears_previous(db_session):
    batch = _make_batch(db_session)
    # Make doc_b a child of doc_a so they end up in the same sub-group.
    doc_a = _make_doc(db_session, batch.id, "A")
    doc_b = _make_doc(db_session, batch.id, "B", parent_id=doc_a.id)
    set_cover_letter(db_session, doc_a.id, batch.id)
    db_session.refresh(doc_a)
    assert doc_a.role == DocumentRole.COVER_LETTER
    set_cover_letter(db_session, doc_b.id, batch.id)
    db_session.refresh(doc_a)
    db_session.refresh(doc_b)
    assert doc_a.role != DocumentRole.COVER_LETTER
    assert doc_b.role == DocumentRole.COVER_LETTER


@pytest.mark.unit
def test_create_sub_group_appends(db_session):
    batch = _make_batch(db_session)
    _make_doc(db_session, batch.id)
    sg = create_sub_group(db_session, batch.id)
    assert sg.batch_id == batch.id
    groups = (
        db_session.query(BatchSubGroup).filter(BatchSubGroup.batch_id == batch.id).all()
    )
    assert len(groups) == 2  # 1 from init + 1 new


@pytest.mark.unit
def test_rename_sub_group(db_session):
    batch = _make_batch(db_session)
    _make_doc(db_session, batch.id)
    groups = ensure_sub_groups_initialized(db_session, batch.id)
    rename_sub_group(db_session, groups[0].id, batch.id, "Custom Label")
    db_session.refresh(groups[0])
    assert groups[0].label == "Custom Label"


@pytest.mark.unit
def test_rename_sub_group_empty_string_clears(db_session):
    batch = _make_batch(db_session)
    _make_doc(db_session, batch.id)
    groups = ensure_sub_groups_initialized(db_session, batch.id)
    rename_sub_group(db_session, groups[0].id, batch.id, "Custom")
    rename_sub_group(db_session, groups[0].id, batch.id, "")
    db_session.refresh(groups[0])
    assert groups[0].label is None


@pytest.mark.unit
def test_reorder_documents(db_session):
    batch = _make_batch(db_session)
    doc_a = _make_doc(db_session, batch.id, "A")
    doc_b = _make_doc(db_session, batch.id, "B")
    groups = ensure_sub_groups_initialized(db_session, batch.id)
    sg_id = groups[0].id
    reorder_documents(db_session, batch.id, [doc_b.id, doc_a.id], sg_id)
    db_session.refresh(doc_a)
    db_session.refresh(doc_b)
    assert doc_b.sub_group_sort_order < doc_a.sub_group_sort_order


@pytest.mark.unit
def test_has_manual_groups_returns_false_when_none(db_session):
    from app.services.intelligence.batch_analyzer import _has_manual_groups

    batch = _make_batch(db_session)
    _make_doc(db_session, batch.id)
    assert _has_manual_groups(batch.id, db_session) is False


@pytest.mark.unit
def test_has_manual_groups_returns_true_after_init(db_session):
    from app.services.intelligence.batch_analyzer import _has_manual_groups

    batch = _make_batch(db_session)
    _make_doc(db_session, batch.id)
    ensure_sub_groups_initialized(db_session, batch.id)
    assert _has_manual_groups(batch.id, db_session) is True
