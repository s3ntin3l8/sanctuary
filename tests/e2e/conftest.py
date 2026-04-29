"""E2E test fixtures.

E2E tests run against a live server. Start the app first:
    make run

Then in a second terminal:
    make test-e2e

Or override the base URL: `make test-e2e PLAYWRIGHT_OPTIONS="--base-url=http://localhost:8001"`.
"""

import os

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


@pytest.fixture
def db_seed():
    """Direct sqlite3 connection for seeding rows the API can't create
    (Claims/DocumentRelationships are AI-driven; no public POST endpoint).

    The tests own unique IDs (uuid suffixes) so they don't collide with
    real data, but each test still cleans up with `cleanup_callbacks`.
    """
    import sqlite3
    from pathlib import Path

    project_root = Path(__file__).parent.parent.parent
    db_path = project_root / "data" / "sanctuary.db"
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
