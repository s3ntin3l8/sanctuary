import pytest


@pytest.fixture(scope="session")
def browser_context_args(browser_context_args):
    """Configure browser context for testing."""
    return {
        **browser_context_args,
    }


@pytest.fixture(scope="session")
def test_app():
    """Create test app for E2E tests."""
    from app.main import app

    return app


@pytest.fixture(scope="session")
def console_errors(page):
    """Capture console errors."""
    errors = []

    def handle_console(msg):
        if msg.type == "error":
            errors.append(msg.text)

    page.on("console", handle_console)
    return errors
