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
        data={
            "case_id": case_id,
            "title": f"E2E Confirm {suffix}",
            "court_name": "AG Hamburg",
        },
        follow_redirects=False,
    )
    assert resp.status_code in (200, 303), f"Case create failed: {resp.status_code}"

    upload = api_client.post(
        "/upload",
        # The route reads form.getlist("files") (plural) — matches
        # upload_form.html's <input name="files" multiple>. Content includes
        # the suffix (not just the filename) so re-runs against a persistent
        # dev DB don't collide with a prior run's leftover doc on the
        # content-hash duplicate check.
        files={
            "files": (
                f"e2e-confirm-{suffix}.txt",
                f"Test content {suffix}".encode(),
                "text/plain",
            )
        },
    )
    assert upload.status_code == 200, f"Upload failed: {upload.status_code}"

    list_resp = api_client.get("/triage")
    assert list_resp.status_code == 200
    body = list_resp.text
    marker = f"e2e-confirm-{suffix}"
    assert marker in body, f"Uploaded doc not visible in triage queue (no {marker!r})"

    return case_id, suffix


def test_triage_confirm_routes_doc_to_case(page: Page, api_client):
    """Upload → triage → Route → pick case in modal → row gone, doc on case."""
    case_id, suffix = _seed_doc_and_case(api_client)

    page.goto("/triage")
    expect(page.locator(f"text=e2e-confirm-{suffix}")).to_be_visible(timeout=10_000)

    row = (
        page.locator("[data-bundle-key]").filter(has_text=f"e2e-confirm-{suffix}").first
    )

    # A freshly-uploaded doc has no AI-suggested case (no classifier runs in
    # this e2e environment), so triage_row.html renders "Route" (action:
    # assign_case) rather than "Confirm bundle" (action: confirm_bundle,
    # which only renders when lead_sub.suggested_case_id is set). Both
    # dispatch the same triage:open-bundle-confirm modal.
    #
    # The row's Route/Confirm branch is decided at the row's own render time
    # from the bundle's pipeline status; the small pipeline-agg span polls
    # itself every 4s but does NOT re-render the row, so a still-"processing"
    # row briefly shows neither button. Reload until it clears (~1s locally).
    route_button = row.get_by_role("button", name="Route")
    for _ in range(10):
        if route_button.is_visible():
            break
        page.wait_for_timeout(500)
        page.reload()
        row = (
            page.locator("[data-bundle-key]")
            .filter(has_text=f"e2e-confirm-{suffix}")
            .first
        )
        route_button = row.get_by_role("button", name="Route")
    route_button.click()

    # The modal's non-batch form (#bundle-confirm-form) holds the case
    # picker <select> — no suggested_case_id means the picker branch
    # (not the pre-confirmed hidden-input branch) is the one rendered.
    form = page.locator("#bundle-confirm-form")
    expect(form).to_be_visible(timeout=5_000)
    form.locator("select[name='case_id']").select_option(value=case_id)
    form.locator("button[type='submit']").click()

    # The row should leave the queue. Bundle row IDs are stable, so wait for
    # disappearance. Generous timeout because /triage/confirm runs sync.
    expect(page.locator(f"text=e2e-confirm-{suffix}")).to_have_count(0, timeout=15_000)

    # Verify the doc landed on the target case via the API.
    case_page = api_client.get(f"/cases/{case_id}")
    assert case_page.status_code == 200
    assert f"e2e-confirm-{suffix}" in case_page.text, (
        f"Doc not visible on case {case_id}'s page after confirm"
    )
