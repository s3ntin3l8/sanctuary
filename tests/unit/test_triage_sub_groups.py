"""Integration tests for BatchSubGroup mutation methods in the triage service."""

import pytest

from app.models.database import BatchSubGroup, Document, IngestBatch
from app.models.enums import DocumentRole, IngestBatchSourceType, IngestBatchStatus
from app.services.triage_subgroups import (
    _group_heading,
    create_sub_group,
    ensure_sub_groups_initialized,
    rename_sub_group,
    reorder_documents,
    set_cover_letter,
)


def _make_batch(db_session) -> IngestBatch:
    batch = IngestBatch(
        source_type=IngestBatchSourceType.EMAIL,
        status=IngestBatchStatus.OPEN,
    )
    db_session.add(batch)
    db_session.flush()
    return batch


def _make_doc(
    db_session,
    batch_id: int,
    title="Doc",
    parent_id=None,
    role=DocumentRole.STANDALONE,
) -> Document:
    doc = Document(
        title=title,
        ingest_batch_id=batch_id,
        parent_id=parent_id,
        role=role,
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
def test_ensure_sub_groups_standalone_docs_share_one_group(db_session):
    """Loose documents share one group — a bundle of attachments is not N groups."""
    batch = _make_batch(db_session)
    docs = [_make_doc(db_session, batch.id, f"Doc {i}") for i in range(3)]

    groups = ensure_sub_groups_initialized(db_session, batch.id)

    assert len(groups) == 1
    for doc in docs:
        db_session.refresh(doc)
        assert doc.sub_group_id == groups[0].id
    assert sorted(d.sub_group_sort_order for d in docs) == [0, 1, 2]


@pytest.mark.unit
def test_ensure_sub_groups_mirrors_the_automatic_layout(db_session):
    """A cover letter keeps its own group; the loose documents share another."""
    batch = _make_batch(db_session)
    cover = _make_doc(
        db_session, batch.id, "Begleitschreiben", role=DocumentRole.COVER_LETTER
    )
    enclosure = _make_doc(
        db_session,
        batch.id,
        "Anlage K 1",
        role=DocumentRole.ENCLOSURE,
        parent_id=cover.id,
    )
    loose = _make_doc(db_session, batch.id, "Kontoauszug")

    groups = ensure_sub_groups_initialized(db_session, batch.id)

    assert len(groups) == 2
    db_session.refresh(cover)
    db_session.refresh(enclosure)
    db_session.refresh(loose)
    assert cover.sub_group_id == enclosure.sub_group_id
    assert loose.sub_group_id != cover.sub_group_id
    # The cover leads its group, the loose document leads the other one.
    assert cover.sub_group_sort_order == 0
    assert loose.sub_group_sort_order == 0


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
def test_set_cover_letter_makes_the_promoted_doc_lead_the_group(db_session):
    """A promoted cover heads its group: it takes the first slot."""
    batch = _make_batch(db_session)
    doc_a = _make_doc(db_session, batch.id, "A")
    doc_b = _make_doc(db_session, batch.id, "B", parent_id=doc_a.id)
    set_cover_letter(db_session, doc_a.id, batch.id)

    set_cover_letter(db_session, doc_b.id, batch.id)

    db_session.refresh(doc_a)
    db_session.refresh(doc_b)
    assert doc_b.role == DocumentRole.COVER_LETTER
    assert doc_b.parent_id is None
    assert doc_a.role == DocumentRole.ENCLOSURE
    assert doc_a.parent_id == doc_b.id
    # The promoted cover leads; the demoted one sits right below it.
    assert doc_b.sub_group_sort_order == 0
    assert doc_a.sub_group_sort_order == 1


@pytest.mark.unit
def test_reorder_documents_keeps_the_cover_letter_first(db_session):
    """The group's cover letter leads the list, whatever order the client sends."""
    batch = _make_batch(db_session)
    doc_a = _make_doc(db_session, batch.id, "A", role=DocumentRole.COVER_LETTER)
    doc_b = _make_doc(db_session, batch.id, "B", parent_id=doc_a.id)
    groups = ensure_sub_groups_initialized(db_session, batch.id)
    sg_id = groups[0].id

    reorder_documents(db_session, batch.id, [doc_b.id, doc_a.id], sg_id)

    db_session.refresh(doc_a)
    db_session.refresh(doc_b)
    assert doc_a.sub_group_sort_order == 0
    assert doc_b.sub_group_sort_order == 1


@pytest.mark.unit
def test_group_heading_is_the_cover_letter_that_heads_the_group(db_session):
    """A cover letter nested under another document is an enclosure, not a heading."""
    batch = _make_batch(db_session)
    cover = _make_doc(db_session, batch.id, "A", role=DocumentRole.COVER_LETTER)
    nested = _make_doc(
        db_session, batch.id, "B", role=DocumentRole.COVER_LETTER, parent_id=cover.id
    )

    heading = _group_heading([cover, nested])

    assert heading is not None
    assert heading.id == cover.id


@pytest.mark.unit
def test_reorder_documents_pins_a_cover_letter_that_moves_in(db_session):
    """A cover letter that joins a group heads it right away."""
    batch = _make_batch(db_session)
    cover = _make_doc(db_session, batch.id, "A", role=DocumentRole.COVER_LETTER)
    other = _make_doc(db_session, batch.id, "B")
    groups = ensure_sub_groups_initialized(db_session, batch.id)
    target = groups[-1].id

    reorder_documents(db_session, batch.id, [other.id, cover.id], target)

    db_session.refresh(cover)
    db_session.refresh(other)
    assert cover.sub_group_id == target
    assert cover.sub_group_sort_order == 0
    assert other.sub_group_sort_order == 1


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
