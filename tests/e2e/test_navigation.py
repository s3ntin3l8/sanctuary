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


def test_navigation_to_settings_via_profile_menu(page: Page):
    """Settings is reached from the profile menu at the bottom of the rail."""
    page.click('button[aria-label="Account menu"]')
    page.click('a[href="/settings/account"]')
    expect(page).to_have_url("**/settings/account")


def test_home_renders_dashboard(page: Page):
    """The SPA home shows the greeting header and the active-cases panel."""
    expect(page.get_by_role("heading", level=1)).to_contain_text("Good")
    expect(page.get_by_text("Active cases", exact=False).first).to_be_visible()


def test_sidebar_present(page: Page):
    """Test the icon rail is present."""
    expect(page.get_by_role("navigation", name="Primary")).to_be_visible()


def test_no_console_errors(page: Page, console_errors):
    """Test no console errors on page load."""
    assert len(console_errors) == 0, f"Console errors found: {console_errors}"
