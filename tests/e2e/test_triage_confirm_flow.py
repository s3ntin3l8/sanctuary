"""E2E: triage confirm flow.

Journey: a doc lands in the Triage Inbox → user opens its expanded body →
clicks Confirm with a target case → bundle row leaves the queue → doc
appears under the case.

This pins the highest-frequency happy-path workflow described in
CLAUDE.md (`Triage is a strategy session`). Drives the unified
`/triage/document/{id}/confirm` endpoint via the UI.
"""

import uuid

import pytest
from playwright.sync_api import Page, expect

pytestmark = pytest.mark.e2e


def _seed_doc_and_case(api_client) -> tuple[str, int]:
    """Create a target Case and a triage Document via the live API.

    Returns (case_id, doc_id). Uses unique IDs to avoid collision with
    leftover data from prior runs.
    """
    suffix = uuid.uuid4().hex[:6].upper()
    case_id = f"E2E-CONF-{suffix}"

    resp = api_client.post(
        "/cases",
        data={"case_id": case_id, "title": f"E2E Confirm {suffix}"},
        follow_redirects=False,
    )
    assert resp.status_code in (200, 303), f"Case create failed: {resp.status_code}"

    upload = api_client.post(
        "/upload",
        files={"file": (f"e2e-confirm-{suffix}.txt", b"Test content", "text/plain")},
    )
    assert upload.status_code == 200, f"Upload failed: {upload.status_code}"

    list_resp = api_client.get("/triage")
    assert list_resp.status_code == 200
    body = list_resp.text
    marker = f"e2e-confirm-{suffix}"
    assert marker in body, f"Uploaded doc not visible in triage queue (no {marker!r})"

    return case_id, suffix


def test_triage_confirm_routes_doc_to_case(page: Page, api_client):
    """Upload → triage → expand row → set case → Confirm → row gone, doc on case."""
    case_id, suffix = _seed_doc_and_case(api_client)

    page.goto("/triage")
    expect(page.locator(f"text=e2e-confirm-{suffix}")).to_be_visible(timeout=10_000)

    row = (
        page.locator("[data-bundle-key]").filter(has_text=f"e2e-confirm-{suffix}").first
    )
    row.click()

    expanded = page.locator("[id^='triage-row-expanded-']").filter(
        has_text=f"e2e-confirm-{suffix}"
    )
    expect(expanded).to_be_visible(timeout=5_000)

    case_input = (
        page.get_by_label("Case", exact=False)
        .or_(page.locator("input[name='case_id']"))
        .first
    )
    case_input.fill(case_id)

    confirm = page.get_by_role("button", name="Confirm").first.or_(
        page.locator("button[data-confirm-bundle-trigger]").first
    )
    confirm.click()

    # The row should leave the queue. Bundle row IDs are stable, so wait for
    # disappearance. Generous timeout because /triage/confirm runs sync.
    expect(page.locator(f"text=e2e-confirm-{suffix}")).to_have_count(0, timeout=15_000)

    # Verify the doc landed on the target case via the API.
    case_page = api_client.get(f"/cases/{case_id}")
    assert case_page.status_code == 200
    assert f"e2e-confirm-{suffix}" in case_page.text, (
        f"Doc not visible on case {case_id}'s page after confirm"
    )
