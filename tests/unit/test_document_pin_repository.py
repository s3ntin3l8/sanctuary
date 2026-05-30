import pytest

from app.models.database import Case, CaseStatus, Document, DocumentPin
from app.repositories.document_pin import DocumentPinRepository
from app.services.document_service import DocumentService


@pytest.fixture
def doc_with_case(db_session):
    case = Case(id="PIN-TEST-001", title="Pin Test Case", status=CaseStatus.INTAKE)
    db_session.add(case)
    db_session.flush()
    doc = Document(title="Pin Test Doc", case_id="PIN-TEST-001")
    db_session.add(doc)
    db_session.commit()
    return doc


@pytest.mark.unit
def test_pin_create(db_session, doc_with_case, sample_user):
    repo = DocumentPinRepository(db_session)
    pin = repo.create(
        doc_with_case.id,
        "abc123456789",
        note="Important passage",
        user_id=sample_user.id,
    )
    db_session.commit()

    assert pin.id is not None
    assert pin.document_id == doc_with_case.id
    assert pin.passage_id == "abc123456789"
    assert pin.note == "Important passage"
    assert pin.user_id == sample_user.id


@pytest.mark.unit
def test_pin_create_no_note(db_session, doc_with_case, sample_user):
    repo = DocumentPinRepository(db_session)
    pin = repo.create(doc_with_case.id, "abc123456789", user_id=sample_user.id)
    db_session.commit()

    assert pin.note is None


@pytest.mark.unit
def test_pin_get(db_session, doc_with_case, sample_user):
    repo = DocumentPinRepository(db_session)
    created = repo.create(
        doc_with_case.id, "abc123456789", note="Test", user_id=sample_user.id
    )
    db_session.commit()

    fetched = repo.get(created.id)
    assert fetched is not None
    assert fetched.id == created.id


@pytest.mark.unit
def test_pin_get_nonexistent(db_session):
    repo = DocumentPinRepository(db_session)
    assert repo.get(99999) is None


@pytest.mark.unit
def test_pin_get_by_document(db_session, doc_with_case, sample_user):
    repo = DocumentPinRepository(db_session)
    repo.create(doc_with_case.id, "pid1", note="First", user_id=sample_user.id)
    repo.create(doc_with_case.id, "pid2", note="Second", user_id=sample_user.id)
    db_session.commit()

    pins = repo.get_by_document(doc_with_case.id)
    assert len(pins) == 2
    # Ordered by ingest_date asc
    assert pins[0].passage_id == "pid1"
    assert pins[1].passage_id == "pid2"


@pytest.mark.unit
def test_pin_get_by_document_empty(db_session, doc_with_case):
    repo = DocumentPinRepository(db_session)
    assert repo.get_by_document(doc_with_case.id) == []


@pytest.mark.unit
def test_pin_update_note(db_session, doc_with_case, sample_user):
    repo = DocumentPinRepository(db_session)
    pin = repo.create(
        doc_with_case.id, "abc123456789", note="Original", user_id=sample_user.id
    )
    db_session.commit()

    updated = repo.update_note(pin.id, "Updated note")
    db_session.commit()

    assert updated is not None
    assert updated.note == "Updated note"


@pytest.mark.unit
def test_pin_update_note_nonexistent(db_session):
    repo = DocumentPinRepository(db_session)
    assert repo.update_note(99999, "Nope") is None


@pytest.mark.unit
def test_pin_delete(db_session, doc_with_case, sample_user):
    repo = DocumentPinRepository(db_session)
    pin = repo.create(doc_with_case.id, "abc123456789", user_id=sample_user.id)
    db_session.commit()

    result = repo.delete(pin.id)
    db_session.commit()

    assert result is True
    assert repo.get(pin.id) is None


@pytest.mark.unit
def test_pin_delete_nonexistent(db_session):
    repo = DocumentPinRepository(db_session)
    assert repo.delete(99999) is False


@pytest.mark.unit
def test_passage_pin_counts_multi_pin(db_session, doc_with_case, sample_user):
    """Multiple pins on the same passage sum correctly in passage_pin_counts."""
    from app.services.hud_context import build_hud_context

    repo = DocumentPinRepository(db_session)
    repo.create(doc_with_case.id, "pid1", note="A", user_id=sample_user.id)
    repo.create(doc_with_case.id, "pid1", note="B", user_id=sample_user.id)
    repo.create(doc_with_case.id, "pid2", note="C", user_id=sample_user.id)
    db_session.commit()

    ctx = build_hud_context(db_session, doc_with_case)
    counts = ctx["passage_pin_counts"]
    assert counts.get("pid1", 0) == 2
    assert counts.get("pid2", 0) == 1


@pytest.mark.unit
def test_hud_context_includes_pins_key(db_session, doc_with_case):
    from app.services.hud_context import build_hud_context

    ctx = build_hud_context(db_session, doc_with_case)
    assert "pins" in ctx
    assert "passage_pin_counts" in ctx
    assert isinstance(ctx["pins"], list)
    assert isinstance(ctx["passage_pin_counts"], dict)


@pytest.mark.unit
def test_delete_document_removes_pins(db_session, doc_with_case, sample_user):
    """Cascade symmetry: delete_document removes pins for the deleted doc."""
    repo = DocumentPinRepository(db_session)
    repo.create(
        doc_with_case.id, "pid999999999", note="Will be deleted", user_id=sample_user.id
    )
    db_session.commit()

    svc = DocumentService(db_session)
    svc.delete_document(doc_with_case.id)

    remaining = (
        db_session.query(DocumentPin)
        .filter(DocumentPin.document_id == doc_with_case.id)
        .count()
    )
    assert remaining == 0
