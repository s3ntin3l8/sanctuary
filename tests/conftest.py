import fcntl
import logging
import os
import tempfile

os.environ.setdefault("SANCTUARY_LOG_FILE", "0")
# Run Celery tasks inline so the suite needs no broker/Redis. Force (not
# setdefault) this: `make test`/`make lint` `-include .env` + `export`, so a
# local .env with CELERY_TASK_ALWAYS_EAGER=false (set for `make run`/`make
# worker` against a real broker) would otherwise leak into the test process
# and silently disable eager mode — tasks whose .delay() isn't mocked (e.g.
# metadata_task.delay() in process_document_task) then attempt a real Redis
# connection and fail with a retry-limit RuntimeError. No test in this suite
# exercises real (non-eager) dispatch, so there is nothing to opt out for.
os.environ["CELERY_TASK_ALWAYS_EAGER"] = "true"


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


def _test_db_path() -> str:
    """RAM-backed test DB path.

    `/dev/shm` is tmpfs on Linux, so the suite never fsyncs to disk. Combined
    with the single-run lock below this removes the disk-I/O contention that
    once wedged overlapping `pytest` runs in uninterruptible `D` state. The pid
    in the name is defense-in-depth against stale-file collisions.
    """
    base = (
        "/dev/shm"
        if os.path.isdir("/dev/shm") and os.access("/dev/shm", os.W_OK)
        else tempfile.gettempdir()
    )
    return os.path.join(base, f"sanctuary_test_{os.getpid()}.db")


TEST_DB_PATH = _test_db_path()
TEST_DATABASE_URL = f"sqlite:///{TEST_DB_PATH}"

# --- Single-run lock --------------------------------------------------------
# The suite shares ONE on-disk SQLite database and has no per-process isolation.
# Overlapping `pytest` processes contend on that file; an earlier incident piled
# up ~14 concurrent runs and wedged the machine on disk I/O (D-state, un-killable
# from userspace). This lock makes a second concurrent run fail fast — loudly —
# instead of silently piling on. It lives in conftest (not the Makefile) so it
# fires for every entry point: `make test`, a bare `python -m pytest`, and the
# pre-push hook.
#
# xdist workers (pytest -n auto) are coordinated subprocesses of ONE invocation,
# not a second overlapping run — each gets its own tmpfs DB via the pid-based
# path above, so they don't contend on a shared file. Only the controller (no
# PYTEST_XDIST_WORKER env var) takes the lock; a second, separate `pytest -n
# auto` invocation still collides on it exactly as before.
_RUN_LOCK_PATH = os.path.join(tempfile.gettempdir(), "sanctuary-pytest.lock")
_RUN_LOCK_FD: int | None = None


def pytest_configure(config):
    global _RUN_LOCK_FD
    if "PYTEST_XDIST_WORKER" in os.environ:
        return
    fd = os.open(_RUN_LOCK_PATH, os.O_CREAT | os.O_RDWR, 0o644)
    try:
        fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except BlockingIOError:
        os.close(fd)
        pytest.exit(
            "another test run is active — the suite shares one SQLite DB and "
            "must run serially; run one invocation at a time and wait for it",
            returncode=2,
        )
    _RUN_LOCK_FD = fd


def pytest_unconfigure(config):
    global _RUN_LOCK_FD
    if _RUN_LOCK_FD is not None:
        try:
            fcntl.flock(_RUN_LOCK_FD, fcntl.LOCK_UN)
            os.close(_RUN_LOCK_FD)
        finally:
            _RUN_LOCK_FD = None


def _seed_bootstrap_admin(db):
    """Test-only: create + pin the dev-mode primary admin (admin@localhost).

    Production no longer auto-creates a magic admin (the first-run create-admin
    screen / env provisioning does), so tests seed a deterministic owner here for
    the AUTH_ENABLED=false gate and directly-created docs/batches to bind to.
    """
    from app.models.enums import UserRole
    from app.services import auth_service

    admin = auth_service.get_user_by_email(db, "admin@localhost")
    if admin is None:
        admin = auth_service.create_user(
            db,
            email="admin@localhost",
            password="devpassword123",
            role=UserRole.ADMIN,
            display_name="Administrator",
        )
    auth_service.set_bootstrap_admin_id(db, admin.id)
    return admin


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
        "app.services.ai_run_index",
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
        # No try/except: a real load failure here should raise immediately with
        # a clear traceback, not be swallowed. The CREATE VIRTUAL TABLE below
        # would fail anyway (with a much less clear error) if this didn't work.
        import sqlite_vec

        dbapi_conn.enable_load_extension(True)
        sqlite_vec.load(dbapi_conn)
        dbapi_conn.enable_load_extension(False)

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
                "CREATE VIRTUAL TABLE IF NOT EXISTS document_chunk_vectors USING vec0(chunk_id INTEGER PRIMARY KEY, embedding float[768])"
            )
        )
        conn.commit()
    Base.metadata.create_all(bind=engine)
    yield engine
    # Remove the DB and its WAL/shared-memory sidecars (tmpfs, so they'd vanish
    # on reboot anyway, but keep /dev/shm tidy between runs).
    for suffix in ("", "-wal", "-shm"):
        path = TEST_DB_PATH + suffix
        if os.path.exists(path):
            try:
                os.remove(path)
            except OSError:
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
    with TestingSessionLocal() as seed_db:
        seed_triage_case(seed_db)
        _seed_bootstrap_admin(seed_db)
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
    # Patch get_db_session where background tasks open their own sessions, and
    # globally neutralize the EXTRACT pipeline entry so no request handler ever
    # runs it implicitly during tests. Two dispatch surfaces reach
    # process_document_task:
    #   * `.delay`       — the direct caller reingest_all_documents_task.
    #   * `.apply_async` — what dispatch_task() invokes for the upload path.
    # Under CELERY_TASK_ALWAYS_EAGER=true (the suite default) either would run
    # the task body inline — cascading into metadata_task (real AI httpx) and
    # concurrent writes to the shared test SQLite on an unmanaged daemon thread
    # that outlives the test. No-op both so uploads stay "queued, not run".
    fake_dispatch = MagicMock()

    with (
        patch(
            "app.tasks.document_processing.get_db_session",
            side_effect=override_get_db_session,
        ),
        patch(
            "app.tasks.document_processing.process_document_task.delay", fake_dispatch
        ),
        patch(
            "app.tasks.document_processing.process_document_task.apply_async",
            fake_dispatch,
        ),
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
    from app.services.case_service import seed_triage_case

    yield
    db_session.rollback()
    for table in reversed(Base.metadata.sorted_tables):
        db_session.execute(table.delete())
    # The document_chunk_vectors vec0 table is created manually (not in
    # Base.metadata), so the loop above never clears it. Without this, a
    # committed embedding row leaks into later tests and collides on
    # chunk_id (rowids are reused after the wipe) — e.g. a fresh chunk
    # reusing id=1 fails its document_chunk_vectors INSERT.
    from sqlalchemy import text as _sa_text

    db_session.execute(_sa_text("DELETE FROM document_chunk_vectors"))
    db_session.commit()
    # Drop identity-mapped instances of the just-wiped rows so re-seeding the
    # AppSettings singleton doesn't collide with a stale in-session object.
    db_session.expunge_all()
    seed_triage_case(db_session)
    # Re-seed the dev-mode bootstrap admin so the next test's directly-created
    # docs/batches (defaulted to it via the before_insert listener) are visible.
    _seed_bootstrap_admin(db_session)
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


@pytest.fixture
def mock_dispatch_task():
    """Opt-in: intercept the request handler's background pipeline dispatch.

    The EXTRACT pipeline body is already globally inert in tests — `setup_test_db`
    no-ops `process_document_task.{delay,apply_async}`, so nothing runs the real
    pipeline regardless of dispatch path. This fixture is a finer instrument for
    the upload tests: patching `dispatch_task` at its source lets them assert the
    endpoint *queued* the run (wiring intact) while keeping it from spawning even
    the (now harmless) eager daemon thread, so the assertion is race-free.

    documents.py imports dispatch_task lazily inside the handler, so the source
    patch covers the upload path. Not autouse: tests that exercise dispatch_task's
    real forwarding (e.g. recover_unclaimed_ready_batches) must keep the genuine
    function.
    """
    with patch("app.tasks.dispatch.dispatch_task") as mock:
        yield mock


@pytest.fixture(autouse=True)
def clear_cache():
    from app.core.cache import cache

    cache.clear()


@pytest.fixture
def app_client():
    from fastapi.testclient import TestClient

    from app.main import app

    return TestClient(app)
