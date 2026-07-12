"""E2E test fixtures.

E2E tests run against a live server. Two ways to provide one:

1. Your own dev server + dev DB (`data/sanctuary.db`) -- start it first:
       make run
   Then in a second terminal:
       make test-e2e
   Or override the base URL: `make test-e2e PLAYWRIGHT_OPTIONS="--base-url=http://localhost:8001"`.

2. A throwaway, fully isolated server + DB (mirrors CI's e2e job) -- never
   touches `data/sanctuary.db`:
       make test-e2e-isolated
   Use this whenever you don't want test data landing in your real dev DB
   (this is what a stray/manual local repro should always use instead).
"""

import os
from pathlib import Path

import httpx
import pytest


def _server_url() -> str:
    return os.getenv("E2E_BASE_URL", "http://127.0.0.1:8000")


@pytest.fixture(scope="session", autouse=True)
def require_running_server():
    """Skip the whole e2e module when the dev server isn't reachable.

    Without this, every test fails with a confusing connection-refused error
    instead of a clear "start `make run` first" signal. We accept any HTTP
    response (incl. 404) — server is up if it answers at all.
    """
    url = _server_url()
    try:
        httpx.get(url, timeout=2.0)
    except httpx.HTTPError:
        pytest.skip(f"E2E server not reachable at {url} — run `make run` first")


@pytest.fixture(scope="session")
def base_url():
    """Override pytest-playwright's base_url so tests can use `page.goto('/')`."""
    return _server_url()


@pytest.fixture(scope="session")
def browser_context_args(browser_context_args, base_url):
    """Configure browser context for testing."""
    return {
        **browser_context_args,
        "base_url": base_url,
    }


@pytest.fixture(scope="session")
def api_client():
    """HTTP client for seeding test data via the live API."""
    with httpx.Client(base_url=_server_url(), timeout=10.0) as client:
        yield client


def _e2e_db_path() -> Path:
    """Resolve the sqlite file this fixture's raw connection should target.

    Must always point at the exact same database the live server (whatever
    `E2E_BASE_URL` answers) is using, or seeded rows / assertions silently
    operate on the wrong file. Defaults to `data/sanctuary.db` -- matching
    `make run`'s own default (app/config.py) -- for the documented
    `make run` + `make test-e2e` workflow. Set DATABASE_URL (as
    `make test-e2e-isolated` does, pointing at a throwaway DB) to redirect
    both the server and this fixture together; never change one without the
    other.
    """
    database_url = os.getenv("DATABASE_URL")
    if database_url and database_url.startswith("sqlite:///"):
        return Path(database_url.removeprefix("sqlite:///"))

    project_root = Path(__file__).parent.parent.parent
    return project_root / "data" / "sanctuary.db"


@pytest.fixture
def db_seed():
    """Direct sqlite3 connection for seeding rows the API can't create
    (Claims/DocumentRelationships are AI-driven; no public POST endpoint).

    The tests own unique IDs (uuid suffixes) so they don't collide with
    real data, but each test still cleans up with `cleanup_callbacks`.
    """
    import sqlite3

    db_path = _e2e_db_path()
    if not db_path.exists():
        pytest.skip(f"E2E DB not found at {db_path} — run `make migrate` first")

    conn = sqlite3.connect(str(db_path))
    conn.execute("PRAGMA foreign_keys=ON")
    cleanup_callbacks: list = []
    try:
        yield conn, cleanup_callbacks
    finally:
        for cb in reversed(cleanup_callbacks):
            try:
                cb(conn)
            except Exception:
                pass
        conn.commit()
        conn.close()


@pytest.fixture
def console_errors(page):
    """Capture console errors. Function-scoped because `page` is."""
    errors = []

    def handle_console(msg):
        if msg.type == "error":
            errors.append(msg.text)

    page.on("console", handle_console)
    return errors
