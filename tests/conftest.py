import logging
import os

os.environ.setdefault("SANCTUARY_LOG_FILE", "0")
# Run Celery tasks inline so the suite needs no broker/Redis. The standardized
# ci-python.yml reusable workflow can't inject env vars, so the test suite owns
# this default (matching what the bespoke CI used to set). setdefault lets a real
# worker/dev run override it.
os.environ.setdefault("CELERY_TASK_ALWAYS_EAGER", "true")


def pytest_sessionfinish(session, exitstatus):
    """Register an atexit handler that silences logging during interpreter shutdown.

    torch._subclasses.fake_tensor registers dump_cache_stats() via @atexit.register
    at import time.  That atexit fires *after* pytest has already closed its log-capture
    StreamHandler, causing "--- Logging error ---" noise.

    We register our own atexit handler here (in pytest_sessionfinish, which runs after
    all tests).  Python runs atexit in LIFO order, so ours fires BEFORE torch's.
    We call logging.disable(logging.CRITICAL) — a global manager flag that pytest's
    pytest_unconfigure does NOT restore, unlike per-logger setLevel() calls.
    """
    import atexit

    atexit.register(lambda: logging.disable(logging.CRITICAL))


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


@pytest.fixture(scope="session", autouse=True)
def isolate_data_dir(tmp_path_factory):
    """Redirect DATA_DIR to a session tmp dir so tests don't pollute ./data/."""
    tmp_data = tmp_path_factory.mktemp("data_session")
    mpatch = pytest.MonkeyPatch()

    import app.config

    mpatch.setattr(app.config, "DATA_DIR", tmp_data)
    # Per-user scan folders are created under SCAN_INCOMING_DIR on user creation;
    # redirect it so tests don't create dirs under the real ./data/scans/.
    scan_incoming = tmp_data / "scans" / "incoming"
    scan_incoming.mkdir(parents=True, exist_ok=True)
    mpatch.setattr(app.config, "SCAN_INCOMING_DIR", scan_incoming)

    for modname in (
        "app.services.ingestion.service",
        "app.services.ingestion.batch_orchestrator",
        "app.services.intelligence._ai_call",
    ):
        mod = __import__(modname, fromlist=["DATA_DIR"])
        if hasattr(mod, "DATA_DIR"):
            mpatch.setattr(mod, "DATA_DIR", tmp_data)

    yield tmp_data
    mpatch.undo()


@pytest.fixture(scope="session")
def test_engine():
    engine = create_engine(TEST_DATABASE_URL, connect_args={"check_same_thread": False})
    # Create virtual tables manually as Base.metadata.create_all doesn't support them
    from sqlalchemy import event as sa_event
    from sqlalchemy import text

    def _load_extensions(dbapi_conn, _):
        try:
            import sqlite_vec

            dbapi_conn.enable_load_extension(True)
            sqlite_vec.load(dbapi_conn)
            dbapi_conn.enable_load_extension(False)
        except Exception:
            pass

        # Mirror production PRAGMA settings so cascade FK behaviour, etc. are
        # actually enforced under tests. Without this, `Document.case_id`'s
        # ondelete=SET NULL is advisory in tests and bugs slip through CI.
        cur = dbapi_conn.cursor()
        cur.execute("PRAGMA foreign_keys=ON")
        cur.close()

    sa_event.listen(engine, "connect", _load_extensions)

    with engine.connect() as conn:
        conn.execute(
            text(
                "CREATE VIRTUAL TABLE IF NOT EXISTS document_vectors USING vec0(document_id INTEGER PRIMARY KEY, embedding float[768])"
            )
        )
        conn.commit()
    Base.metadata.create_all(bind=engine)
    yield engine
    if os.path.exists(TEST_DB_PATH):
        try:
            os.remove(TEST_DB_PATH)
        except PermissionError:
            pass


@pytest.fixture(scope="session", autouse=True)
def setup_test_db(test_engine):
    from app.services.case_service import seed_triage_case

    Base.metadata.create_all(bind=test_engine)
    TestingSessionLocal = sessionmaker(
        autocommit=False, autoflush=False, bind=test_engine
    )

    # Point the production SessionLocal at the test engine so code that opens a
    # session directly (e.g. the AuthGateMiddleware) uses the test DB instead of
    # the real one. FastAPI's get_db is also overridden below for Depends paths.
    import app.config as _app_config
    import app.dependencies as _app_deps

    _orig_deps_sl = _app_deps.SessionLocal
    _orig_cfg_sl = _app_config.SessionLocal
    _app_deps.SessionLocal = TestingSessionLocal
    _app_config.SessionLocal = TestingSessionLocal
    # Seed `_TRIAGE` + the dev-mode bootstrap admin once at session start.
    # `cleanup_per_test` re-seeds after every wipe so subsequent tests do too.
    from app.services import auth_service as _auth_service

    with TestingSessionLocal() as seed_db:
        seed_triage_case(seed_db)
        _auth_service.get_or_create_bootstrap_admin(seed_db)
        seed_db.commit()

    # TEST-ONLY: documents/batches created directly in tests (without going
    # through the ingestion entry points) default their owner to the dev-mode
    # bootstrap admin — the same user the AUTH_ENABLED=false gate binds — so the
    # per-user triage feed/guards see them. Tests that assert isolation set
    # owner_id explicitly, which this leaves untouched.
    from sqlalchemy import event as _sa_event
    from sqlalchemy import text as _sa_text

    from app.models.database import Document as _Doc
    from app.models.database import IngestBatch as _Batch

    def _default_owner(mapper, connection, target):
        if getattr(target, "owner_id", None) is None:
            row = connection.execute(
                _sa_text(
                    "SELECT id FROM users WHERE email = 'admin@localhost' "
                    "ORDER BY id LIMIT 1"
                )
            ).first()
            if row:
                target.owner_id = row[0]

    _sa_event.listen(_Doc, "before_insert", _default_owner)
    _sa_event.listen(_Batch, "before_insert", _default_owner)

    def override_get_db():
        db = TestingSessionLocal()
        try:
            yield db
        finally:
            db.close()

    def override_get_db_session():
        return TestingSessionLocal()

    app.dependency_overrides[get_db] = override_get_db
    # We also need to patch get_db_session where it's used in background tasks.
    # Also no-op `process_document_task.delay` at every import site: with
    # CELERY_TASK_ALWAYS_EAGER=true (the project's `.env` default) every upload
    # would otherwise fire a real httpx call to the configured AI provider
    # (Ollama / LM Studio) and hang when the provider isn't reachable.
    fake_delay = MagicMock()

    with (
        patch(
            "app.tasks.document_processing.get_db_session",
            side_effect=override_get_db_session,
        ),
        patch("app.tasks.document_processing.process_document_task.delay", fake_delay),
        patch("app.api.documents.process_document_task.delay", fake_delay),
    ):
        yield
    app.dependency_overrides.clear()
    _sa_event.remove(_Doc, "before_insert", _default_owner)
    _sa_event.remove(_Batch, "before_insert", _default_owner)
    _app_deps.SessionLocal = _orig_deps_sl
    _app_config.SessionLocal = _orig_cfg_sl
    test_engine.dispose()


@pytest.fixture(autouse=True)
def auth_disabled_by_default(monkeypatch):
    """Default every test to single-user dev mode (AUTH_ENABLED=false).

    The auth gate then passes requests through and the bootstrap admin is bound
    lazily, so the existing endpoint suite keeps working. Auth-specific tests
    opt back in with the `auth_enabled` fixture.
    """
    import app.config as _app_config

    monkeypatch.setattr(_app_config, "AUTH_ENABLED", False)


@pytest.fixture(autouse=True)
def disable_rate_limiter():
    """Disable slowapi limits during tests — its in-memory counter is process-
    global and would otherwise leak across tests that repeatedly POST /login."""
    from app.core.rate_limit import limiter

    prev = limiter.enabled
    limiter.enabled = False
    yield
    limiter.enabled = prev


@pytest.fixture
def auth_enabled(monkeypatch):
    """Turn authentication on for a test (real login gate)."""
    import app.config as _app_config

    monkeypatch.setattr(_app_config, "AUTH_ENABLED", True)
    return True


@pytest.fixture(autouse=True)
def cleanup_per_test(db_session):
    """Clean up data after each test, then re-seed the `_TRIAGE` singleton.

    With FK enforcement on (PRAGMA foreign_keys=ON in test_engine), every
    Document/IngestBatch row that uses `case_id="_TRIAGE"` requires a real
    Case row. The wipe removes it; this re-seeds so the next test starts
    in the same state production lifespan would.
    """
    from app.services import auth_service
    from app.services.case_service import seed_triage_case

    yield
    db_session.rollback()
    for table in reversed(Base.metadata.sorted_tables):
        db_session.execute(table.delete())
    db_session.commit()
    seed_triage_case(db_session)
    # Re-seed the dev-mode bootstrap admin so the next test's directly-created
    # docs/batches (defaulted to it via the before_insert listener) are visible.
    auth_service.get_or_create_bootstrap_admin(db_session)
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
def sample_user(db_session):
    """A regular User for tests that need per-user ownership (reactions, pins,
    per-user settings)."""
    from app.services import auth_service

    user = auth_service.get_user_by_email(db_session, "tester@example.com")
    if user is None:
        user = auth_service.create_user(
            db_session, email="tester@example.com", password="password123"
        )
        db_session.commit()
    return user


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
    # `_TRIAGE` is pre-seeded by `cleanup_per_test`; return the existing row.
    case = db_session.query(Case).filter_by(id="_TRIAGE").one()
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
        patch("app.tasks.generate_case_brief.generate_case_brief_task.apply_async"),
        patch("app.tasks.generate_case_brief.refresh_case_brief_task.delay"),
        patch("app.tasks.generate_case_brief.refresh_case_brief_task.apply_async"),
    ):
        yield


@pytest.fixture(autouse=True)
def clear_cache():
    from app.core.cache import cache

    cache.clear()


@pytest.fixture
def app_client():
    from fastapi.testclient import TestClient

    from app.main import app

    return TestClient(app)
