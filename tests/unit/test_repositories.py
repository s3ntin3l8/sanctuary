import pytest

from app.models.enums import (
    CaseStatus,
    CostCategory,
)


@pytest.mark.unit
def test_case_repository_create(sample_case):
    assert sample_case.id == "TEST-001"
    assert sample_case.title == "Test Case"
    assert sample_case.status == CaseStatus.INTAKE


@pytest.mark.unit
def test_case_repository_get_by_id(sample_case):
    from app.repositories.case import CaseRepository

    repo = CaseRepository(sample_case._sa_instance_state.session)
    result = repo.get_by_id("TEST-001")
    assert result is not None
    assert result.id == "TEST-001"


@pytest.mark.unit
def test_case_repository_get_all_active(sample_case):
    from app.repositories.case import CaseRepository

    repo = CaseRepository(sample_case._sa_instance_state.session)
    results = repo.get_all_active()
    assert len(results) >= 1


@pytest.mark.unit
def test_case_repository_exists(sample_case):
    from app.repositories.case import CaseRepository

    repo = CaseRepository(sample_case._sa_instance_state.session)
    assert repo.exists("TEST-001") is True
    assert repo.exists("NON-EXISTENT") is False


@pytest.mark.unit
def test_case_repository_update_status(sample_case):
    from app.repositories.case import CaseRepository

    repo = CaseRepository(sample_case._sa_instance_state.session)
    result = repo.update_status("TEST-001", CaseStatus.CLOSED)
    assert result is not None
    assert result.status == CaseStatus.CLOSED
    assert result.closed_at is not None


@pytest.mark.unit
def test_document_repository_create(sample_document):
    assert sample_document.title == "Test Document"
    assert sample_document.case_id == "TEST-001"


@pytest.mark.unit
def test_document_repository_get_by_case(sample_document):
    from app.repositories.document import DocumentRepository

    repo = DocumentRepository(sample_document._sa_instance_state.session)
    results = repo.get_by_case("TEST-001")
    assert len(results) >= 1


@pytest.mark.unit
def test_document_repository_get_triage_documents(sample_triage_case):
    from app.models.database import Document
    from app.repositories.document import DocumentRepository

    db = sample_triage_case._sa_instance_state.session
    doc = Document(title="Triage Doc", case_id="_TRIAGE")
    db.add(doc)
    db.commit()

    repo = DocumentRepository(db)
    results = repo.get_triage_documents()
    assert len(results) >= 1


@pytest.mark.unit
def test_document_repository_get_pending_review(sample_document):
    from app.repositories.document import DocumentRepository

    repo = DocumentRepository(sample_document._sa_instance_state.session)
    results = repo.get_pending_review()
    assert isinstance(results, list)


@pytest.mark.unit
def test_deadline_action_item_create(sample_deadline):
    assert sample_deadline.title == "Test Deadline"
    assert sample_deadline.case_id == "TEST-001"


@pytest.mark.unit
def test_action_item_repository_get_by_case(sample_deadline):
    from app.models.enums import ActionItemType
    from app.repositories.action_item import ActionItemRepository

    repo = ActionItemRepository(sample_deadline._sa_instance_state.session)
    results = repo.get_by_case("TEST-001", action_type=ActionItemType.DEADLINE)
    assert len(results) >= 1


@pytest.mark.unit
def test_action_item_repository_mark_completed(sample_deadline):
    from app.models.enums import ActionItemStatus
    from app.repositories.action_item import ActionItemRepository

    repo = ActionItemRepository(sample_deadline._sa_instance_state.session)
    result = repo.mark_completed(sample_deadline.id)
    assert result is not None
    assert result.status == ActionItemStatus.COMPLETED


@pytest.mark.unit
def test_hearing_action_item_create(sample_hearing):
    assert sample_hearing.title == "Test Hearing"
    assert sample_hearing.case_id == "TEST-001"


@pytest.mark.unit
def test_action_item_repository_court_date_by_case(sample_hearing):
    from app.models.enums import ActionItemType
    from app.repositories.action_item import ActionItemRepository

    repo = ActionItemRepository(sample_hearing._sa_instance_state.session)
    results = repo.get_by_case("TEST-001", action_type=ActionItemType.COURT_DATE)
    assert len(results) >= 1


@pytest.mark.unit
def test_action_item_repository_upcoming_court_dates(sample_hearing):
    from app.models.enums import ActionItemType
    from app.repositories.action_item import ActionItemRepository

    repo = ActionItemRepository(sample_hearing._sa_instance_state.session)
    results = repo.get_upcoming(days=365, action_type=ActionItemType.COURT_DATE)
    assert isinstance(results, list)


@pytest.mark.unit
def test_legal_cost_repository_create(sample_cost):
    assert sample_cost.title == "Test Cost"
    assert sample_cost.category == CostCategory.ANWALTSKOSTEN


@pytest.mark.unit
def test_legal_cost_repository_get_by_case(sample_cost):
    from app.repositories.legal_cost import LegalCostRepository

    repo = LegalCostRepository(sample_cost._sa_instance_state.session)
    results = repo.get_by_case("TEST-001")
    assert len(results) >= 1


@pytest.mark.unit
def test_legal_cost_repository_sum_amounts(sample_cost):
    from app.repositories.legal_cost import LegalCostRepository

    repo = LegalCostRepository(sample_cost._sa_instance_state.session)
    sums = repo.sum_amounts_by_case("TEST-001")
    assert "net" in sums
    assert "gross" in sums
    assert sums["net"] == 500.0
