import os
from datetime import UTC, datetime
from unittest.mock import MagicMock, patch

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

from app.dependencies import get_db
from app.main import app
from app.models.database import ActionItem, Base, Case, Document, LegalCost
from app.models.enums import (
    ActionItemType,
    CaseStatus,
    CostCategory,
    CostStatus,
    Jurisdiction,
    OriginatorType,
)

TEST_DB_PATH = "./test_sanctuary.db"
TEST_DATABASE_URL = f"sqlite:///{TEST_DB_PATH}"


@pytest.fixture(scope="session")
def test_engine():
    engine = create_engine(TEST_DATABASE_URL, connect_args={"check_same_thread": False})
    Base.metadata.create_all(bind=engine)
    yield engine
    if os.path.exists(TEST_DB_PATH):
        try:
            os.remove(TEST_DB_PATH)
        except PermissionError:
            pass


@pytest.fixture(scope="session", autouse=True)
def setup_test_db(test_engine):
    Base.metadata.create_all(bind=test_engine)
    TestingSessionLocal = sessionmaker(
        autocommit=False, autoflush=False, bind=test_engine
    )

    def override_get_db():
        db = TestingSessionLocal()
        try:
            yield db
        finally:
            db.close()

    app.dependency_overrides[get_db] = override_get_db
    yield
    app.dependency_overrides.clear()
    test_engine.dispose()


@pytest.fixture(autouse=True)
def cleanup_per_test(db_session):
    """Clean up data after each test."""
    yield
    db_session.rollback()
    for table in reversed(Base.metadata.sorted_tables):
        db_session.execute(table.delete())
    db_session.commit()


@pytest.fixture
def db_session(test_engine):
    SessionLocal = sessionmaker(autocommit=False, autoflush=False, bind=test_engine)
    session = SessionLocal()
    try:
        yield session
    finally:
        session.close()


@pytest.fixture
def db_session_factory(test_engine):
    def _factory():
        return sessionmaker(autocommit=False, autoflush=False, bind=test_engine)()

    return _factory


@pytest.fixture
def sample_case(db_session) -> Case:
    case = Case(
        id="TEST-001",
        title="Test Case",
        status=CaseStatus.INTAKE,
        jurisdiction=Jurisdiction.DE,
    )
    db_session.add(case)
    db_session.commit()
    db_session.refresh(case)
    return case


@pytest.fixture
def sample_triage_case(db_session) -> Case:
    case = Case(
        id="_TRIAGE",
        title="Triage Inbox",
        status=CaseStatus.INTAKE,
        jurisdiction=Jurisdiction.DE,
    )
    db_session.add(case)
    db_session.commit()
    db_session.refresh(case)
    return case


@pytest.fixture
def sample_document(db_session, sample_case) -> Document:
    doc = Document(
        title="Test Document",
        content="Test content",
        case_id=sample_case.id,
        originator_type=OriginatorType.COURT,
        sender="test@example.com",
    )
    db_session.add(doc)
    db_session.commit()
    db_session.refresh(doc)
    return doc


@pytest.fixture
def sample_deadline(db_session, sample_case) -> ActionItem:
    deadline = ActionItem(
        case_id=sample_case.id,
        title="Test Deadline",
        due_date=datetime(2025, 12, 31, 23, 59, tzinfo=UTC),
        action_type=ActionItemType.DEADLINE,
    )
    db_session.add(deadline)
    db_session.commit()
    db_session.refresh(deadline)
    return deadline


@pytest.fixture
def sample_hearing(db_session, sample_case) -> ActionItem:
    hearing = ActionItem(
        case_id=sample_case.id,
        title="Test Hearing",
        due_date=datetime(2025, 6, 15, 10, 0, tzinfo=UTC),
        action_type=ActionItemType.COURT_DATE,
    )
    db_session.add(hearing)
    db_session.commit()
    db_session.refresh(hearing)
    return hearing


@pytest.fixture
def sample_cost(db_session, sample_case) -> LegalCost:
    cost = LegalCost(
        case_id=sample_case.id,
        category=CostCategory.ANWALTSKOSTEN,
        status=CostStatus.OFFEN,
        title="Test Cost",
        amount_net=500.0,
        amount_gross=595.0,
    )
    db_session.add(cost)
    db_session.commit()
    db_session.refresh(cost)
    return cost


@pytest.fixture
def multiple_cases(db_session) -> list[Case]:
    cases = [
        Case(
            id="TEST-001",
            title="Alpha Case",
            status=CaseStatus.INTAKE,
            jurisdiction=Jurisdiction.DE,
        ),
        Case(
            id="TEST-002",
            title="Beta Case",
            status=CaseStatus.DISCOVERY,
            jurisdiction=Jurisdiction.DE,
        ),
        Case(
            id="TEST-003",
            title="Gamma Case",
            status=CaseStatus.CLOSED,
            jurisdiction=Jurisdiction.UK,
        ),
    ]
    for case in cases:
        db_session.add(case)
    db_session.commit()
    for case in cases:
        db_session.refresh(case)
    return cases


@pytest.fixture(autouse=True)
def mock_converter():
    with patch("app.services.ingestion.converters._get_converter") as mock_get:
        mock_conv = MagicMock()
        mock_res = MagicMock()
        mock_doc = MagicMock()
        mock_doc.export_to_markdown.return_value = (
            "# Mocked Document\n\nThis is a test document."
        )
        # convert_file() builds a metadata dict from result.document.pages (len-able)
        # and result.input.format.value (string). Without these overrides MagicMock
        # returns nested mocks that aren't JSON-serialisable and blow up on the
        # Document.meta commit.
        mock_doc.pages = []
        mock_res.document = mock_doc
        mock_res.input.format.value = "PDF"
        mock_conv.convert.return_value = mock_res
        mock_get.return_value = mock_conv
        yield mock_conv


@pytest.fixture(autouse=True)
def mock_phase4_celery_tasks():
    """Prevent Phase 4 Celery tasks from connecting to Redis during tests."""
    with (
        patch("app.tasks.analyze_batch.analyze_batch_task.delay"),
        patch("app.tasks.enrich_document.enrich_document_task.delay"),
        patch("app.tasks.detect_relationships.detect_relationships_task.delay"),
        patch("app.tasks.extract_claims.extract_claims_task.delay"),
        patch("app.tasks.thread_open_scan.thread_open_scan_task.delay"),
        patch("app.tasks.enrich_document.enrich_document_task.apply_async"),
        patch("app.tasks.analyze_batch.analyze_batch_task.apply_async"),
        patch("app.tasks.extract_claims.extract_claims_task.apply_async"),
        patch("app.tasks.scan_ingest.scan_folder_tick_task.delay"),
        patch("app.tasks.scan_ingest.scan_folder_tick_task.apply_async"),
        patch("app.tasks.prepare_slicing.prepare_slicing_task.delay"),
        patch("app.tasks.prepare_slicing.prepare_slicing_task.apply_async"),
        patch("app.tasks.generate_case_brief.generate_case_brief_task.delay"),
        patch("app.tasks.generate_case_brief.refresh_case_brief_task.delay"),
    ):
        yield


@pytest.fixture(autouse=True)
def clear_cache():
    from app.core.cache import cache

    cache.clear()


@pytest.fixture
def mock_ai_services():
    with (
        patch("app.services.ai_summary.summarize_document") as mock_sum,
        patch("app.services.embeddings.generate_embedding") as mock_emb,
        patch("app.services.ai_summary.check_ollama_status") as mock_check_sum,
        patch("app.services.embeddings.check_embedding_status") as mock_check_emb,
    ):
        mock_sum.return_value = MagicMock()
        mock_emb.return_value = [0.1] * 768
        mock_check_sum.return_value = {"status": "ok", "model": "test"}
        mock_check_emb.return_value = {"status": "ok", "model": "test"}

        yield {
            "summarize": mock_sum,
            "embedding": mock_emb,
            "check_sum": mock_check_sum,
            "check_emb": mock_check_emb,
        }


@pytest.fixture
def app_client():
    from fastapi.testclient import TestClient

    from app.main import app

    return TestClient(app)
