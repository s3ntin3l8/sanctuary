from datetime import UTC, datetime, timedelta

import pytest

from app.models.enums import (
    ActionItemStatus,
    ActionItemType,
    CaseStatus,
    OriginatorType,
)


@pytest.fixture
def sample_document(db_session):
    from app.models.database import Case, Document

    case = Case(id="REPO-DOC-001", title="Document Test Case", status=CaseStatus.INTAKE)
    db_session.add(case)
    db_session.flush()

    doc = Document(
        title="Test Document",
        case_id="REPO-DOC-001",
        content="Test content",
        needs_review=True,
        originator_type=OriginatorType.UNKNOWN,
        sender="Test Sender",
    )
    db_session.add(doc)
    db_session.commit()
    db_session.refresh(doc)
    return doc


@pytest.fixture
def sample_deadline(db_session):
    from app.models.database import ActionItem, Case

    case = Case(id="REPO-DL-001", title="Deadline Test Case", status=CaseStatus.INTAKE)
    db_session.add(case)
    db_session.flush()

    deadline = ActionItem(
        case_id="REPO-DL-001",
        title="Test Deadline",
        due_date=datetime.now(UTC) + timedelta(days=7),
        action_type=ActionItemType.DEADLINE,
        status=ActionItemStatus.OPEN,
    )
    db_session.add(deadline)
    db_session.commit()
    db_session.refresh(deadline)
    return deadline


@pytest.fixture
def sample_hearing(db_session):
    from app.models.database import ActionItem, Case

    case = Case(id="REPO-H-001", title="Hearing Test Case", status=CaseStatus.INTAKE)
    db_session.add(case)
    db_session.flush()

    hearing = ActionItem(
        case_id="REPO-H-001",
        title="Test Hearing",
        due_date=datetime.now(UTC) + timedelta(days=14),
        action_type=ActionItemType.COURT_DATE,
        status=ActionItemStatus.OPEN,
        location="Test Room",
    )
    db_session.add(hearing)
    db_session.commit()
    db_session.refresh(hearing)
    return hearing


@pytest.fixture
def sample_entity(db_session):
    from app.models.database import Case, Entity
    from app.models.enums import EntityType

    case = Case(id="REPO-E-001", title="Entity Test Case", status=CaseStatus.INTAKE)
    db_session.add(case)
    db_session.flush()

    entity = Entity(
        case_id="REPO-E-001",
        type=EntityType.PERSON,
        name="Test Person",
    )
    db_session.add(entity)
    db_session.commit()
    db_session.refresh(entity)
    return entity


# Document Repository Tests


@pytest.mark.unit
def test_document_repository_get_by_id(sample_document):
    from app.repositories.document import DocumentRepository

    repo = DocumentRepository(sample_document._sa_instance_state.session)
    result = repo.get(sample_document.id)
    assert result is not None
    assert result.title == "Test Document"


@pytest.mark.unit
def test_document_repository_get_triage_documents(db_session):
    from app.models.database import Document
    from app.repositories.document import DocumentRepository

    triage_doc = Document(
        title="Triage Document",
        case_id="_TRIAGE",
        content="Test content",
        needs_review=True,
        originator_type=OriginatorType.UNKNOWN,
    )
    db_session.add(triage_doc)
    db_session.commit()

    repo = DocumentRepository(db_session)
    results = repo.get_triage_documents()
    assert len(results) >= 1
    triage_doc_ids = [d.id for d in results]
    assert triage_doc.id in triage_doc_ids


@pytest.mark.unit
def test_document_repository_get_by_sender(db_session, sample_document):
    from app.repositories.document import DocumentRepository

    repo = DocumentRepository(db_session)
    results = repo.get_by_sender("Test Sender")
    assert len(results) >= 1
    assert results[0].sender == "Test Sender"


@pytest.mark.unit
def test_document_repository_search(db_session, sample_document):
    from app.repositories.document import DocumentRepository

    repo = DocumentRepository(db_session)
    results = repo.search("Test")
    assert len(results) >= 1


@pytest.mark.unit
def test_document_repository_get_recent(db_session, sample_document):
    from app.repositories.document import DocumentRepository

    repo = DocumentRepository(db_session)
    results = repo.get_recent(limit=10)
    assert len(results) >= 1
    assert results[0].created_at is not None


# ActionItem (formerly Deadline) Repository Tests


@pytest.mark.unit
def test_deadline_action_item_get_by_id(sample_deadline):
    from app.repositories.action_item import ActionItemRepository

    repo = ActionItemRepository(sample_deadline._sa_instance_state.session)
    result = repo.get(sample_deadline.id)
    assert result is not None
    assert result.title == "Test Deadline"


@pytest.mark.unit
def test_deadline_action_item_get_by_case(db_session, sample_deadline):
    from app.repositories.action_item import ActionItemRepository

    repo = ActionItemRepository(db_session)
    results = repo.get_by_case("REPO-DL-001", action_type=ActionItemType.DEADLINE)
    assert len(results) >= 1


@pytest.mark.unit
def test_deadline_action_item_get_upcoming(db_session, sample_deadline):
    from app.repositories.action_item import ActionItemRepository

    repo = ActionItemRepository(db_session)
    results = repo.get_upcoming(days=30, action_type=ActionItemType.DEADLINE)
    assert len(results) >= 1
    assert all(d.status == ActionItemStatus.OPEN for d in results)


@pytest.mark.unit
def test_deadline_action_item_get_overdue(db_session, sample_deadline):
    from app.repositories.action_item import ActionItemRepository

    repo = ActionItemRepository(db_session)
    results = repo.get_overdue(action_type=ActionItemType.DEADLINE)
    assert isinstance(results, list)


# ActionItem (formerly Hearing) Repository Tests


@pytest.mark.unit
def test_hearing_action_item_get_by_id(sample_hearing):
    from app.repositories.action_item import ActionItemRepository

    repo = ActionItemRepository(sample_hearing._sa_instance_state.session)
    result = repo.get(sample_hearing.id)
    assert result is not None
    assert result.title == "Test Hearing"


@pytest.mark.unit
def test_hearing_action_item_get_by_case(db_session, sample_hearing):
    from app.repositories.action_item import ActionItemRepository

    repo = ActionItemRepository(db_session)
    results = repo.get_by_case("REPO-H-001", action_type=ActionItemType.COURT_DATE)
    assert len(results) >= 1


@pytest.mark.unit
def test_hearing_action_item_get_upcoming(db_session, sample_hearing):
    from app.repositories.action_item import ActionItemRepository

    repo = ActionItemRepository(db_session)
    results = repo.get_upcoming(days=30, action_type=ActionItemType.COURT_DATE)
    assert len(results) >= 1
    now = datetime.now(UTC)
    for h in results:
        due = h.due_date if h.due_date.tzinfo else h.due_date.replace(tzinfo=UTC)
        assert due > now


@pytest.mark.unit
def test_hearing_action_item_count_by_case(db_session, sample_hearing):
    from app.repositories.action_item import ActionItemRepository

    repo = ActionItemRepository(db_session)
    count = repo.count_open_by_case("REPO-H-001")
    assert count >= 1


# Entity Repository Tests


@pytest.mark.unit
def test_entity_repository_get_by_id(sample_entity):
    from app.repositories.entity import EntityRepository

    repo = EntityRepository(sample_entity._sa_instance_state.session)
    result = repo.get(sample_entity.id)
    assert result is not None
    assert result.name == "Test Person"


@pytest.mark.unit
def test_entity_repository_get_by_case(db_session, sample_entity):
    from app.repositories.entity import EntityRepository

    repo = EntityRepository(db_session)
    results = repo.get_by_case("REPO-E-001")
    assert len(results) >= 1


@pytest.mark.unit
def test_entity_repository_get_by_type(db_session, sample_entity):
    from app.models.enums import EntityType
    from app.repositories.entity import EntityRepository

    repo = EntityRepository(db_session)
    results = repo.get_by_type(EntityType.PERSON)
    assert len(results) >= 1


# IngestBatch Repository Tests


@pytest.mark.unit
def test_ingest_batch_message_id(db_session):
    from app.models.enums import IngestBatchSourceType
    from app.repositories.ingest_batch import IngestBatchRepository

    repo = IngestBatchRepository(db_session)
    batch = repo.create_batch(
        source_type=IngestBatchSourceType.EMAIL,
        subject="Test",
    )
    batch.message_id = "test-msg-id"
    db_session.commit()

    found = repo.get_by_message_id("test-msg-id")
    assert found is not None
    assert found.id == batch.id
