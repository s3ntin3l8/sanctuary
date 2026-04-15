import os
from unittest.mock import MagicMock, patch

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

from app.dependencies import get_db
from app.main import app
from app.models.database import Base

# Use a test database file
TEST_DB_PATH = "./test_sanctuary.db"
TEST_DATABASE_URL = f"sqlite:///{TEST_DB_PATH}"


def get_test_engine():
    return create_engine(TEST_DATABASE_URL, connect_args={"check_same_thread": False})


@pytest.fixture(scope="session")
def test_engine():
    engine = get_test_engine()
    Base.metadata.create_all(bind=engine)
    yield engine
    if os.path.exists(TEST_DB_PATH):
        try:
            os.remove(TEST_DB_PATH)
        except PermissionError:
            pass


@pytest.fixture(scope="session", autouse=True)
def setup_test_db(test_engine):
    TestingSessionLocal = sessionmaker(
        autocommit=False, autoflush=False, bind=test_engine
    )

    # Create all tables
    Base.metadata.create_all(bind=test_engine)

    # Override the dependency
    def override_get_db():
        db = TestingSessionLocal()
        try:
            yield db
        finally:
            db.close()

    app.dependency_overrides[get_db] = override_get_db
    yield
    app.dependency_overrides.clear()


@pytest.fixture
def db_session(test_engine):
    """Provide a clean database session for each test."""
    SessionLocal = sessionmaker(autocommit=False, autoflush=False, bind=test_engine)
    session = SessionLocal()
    try:
        yield session
    finally:
        session.close()


@pytest.fixture(autouse=True)
def mock_converter():
    """Mock the Docling converter to avoid real PDF processing in tests."""
    with patch("app.services.ingestion._get_converter") as mock_get:
        mock_conv = MagicMock()
        mock_res = MagicMock()
        mock_doc = MagicMock()
        mock_doc.export_to_markdown.return_value = (
            "# Mocked Document\n\nThis is a test document."
        )
        mock_res.document = mock_doc
        mock_conv.convert.return_value = mock_res
        mock_get.return_value = mock_conv
        yield mock_conv


@pytest.fixture(autouse=True)
def mock_ai_services():
    """Mock AI-related services to avoid real Ollama calls and long timeouts."""
    with (
        patch("app.services.ai_summary.summarize_document") as mock_sum,
        patch("app.services.embeddings.generate_embedding") as mock_emb,
        patch("app.services.ai_summary.check_ollama_status") as mock_check_sum,
        patch("app.services.embeddings.check_embedding_status") as mock_check_emb,
    ):
        mock_sum.return_value = MagicMock()  # Return a mock document
        mock_emb.return_value = [0.1] * 768
        mock_check_sum.return_value = {"status": "ok", "model": "test"}
        mock_check_emb.return_value = {"status": "ok", "model": "test"}

        yield {
            "summarize": mock_sum,
            "embedding": mock_emb,
            "check_sum": mock_check_sum,
            "check_emb": mock_check_emb,
        }
