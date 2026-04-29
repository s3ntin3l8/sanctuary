import pytest
from playwright.sync_api import Page, expect


@pytest.fixture
def page(page: Page):
    """Navigate to app home on each test."""
    page.goto("/")
    return page


def test_dashboard_loads(page: Page):
    """Test dashboard loads successfully."""
    expect(page.locator("body")).to_be_visible()
    assert page.title()


def test_navigation_to_cases(page: Page):
    """Test navigation to cases page."""
    page.click('a[href="/cases"]')
    expect(page.locator("body")).to_be_visible()


def test_navigation_to_triage(page: Page):
    """Test navigation to triage page."""
    page.click('a[href="/triage"]')
    expect(page.locator("body")).to_be_visible()


def test_navigation_to_costs(page: Page):
    """Test navigation to costs page."""
    page.click('a[href="/costs"]')
    expect(page.locator("body")).to_be_visible()


def test_navigation_to_contacts(page: Page):
    """Test navigation to contacts page."""
    page.click('a[href="/contacts"]')
    expect(page.locator("body")).to_be_visible()


def test_sidebar_present(page: Page):
    """Test sidebar navigation is present."""
    sidebar = page.locator("nav, aside, [class*='sidebar']")
    expect(sidebar.first).to_be_visible()


def test_no_console_errors(page: Page, console_errors):
    """Test no console errors on page load."""
    assert len(console_errors) == 0, f"Console errors found: {console_errors}"
