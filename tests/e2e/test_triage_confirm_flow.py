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


def _seed_doc_and_case(api_client, db_seed) -> tuple[str, int]:
    """Create a target Case and a triage Document via the live API.

    Returns (case_id, doc_id). Uses unique IDs to avoid collision with
    leftover data from prior runs.
    """
    conn, cleanup = db_seed
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
        # content-hash duplicate check. Must be >= 30 non-whitespace chars —
        # is_valid_docling_output() (converters.py) rejects shorter content
        # as a likely near-empty/placeholder OCR result.
        files={
            "files": (
                f"e2e-confirm-{suffix}.txt",
                f"This is a test document body for e2e confirm flow {suffix}.".encode(),
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

    _force_pipeline_completed(conn, suffix)

    return case_id, suffix


def _force_pipeline_completed(conn, suffix: str) -> None:
    """Fast-forward the seeded doc's pipeline_state to 'completed'.

    mock_status() (triage_view.py) only needs pipeline_state out of
    {pending, running, partial} to unlock the Route/Confirm buttons, so
    force it directly rather than waiting on a real classification result —
    the extract -> metadata pipeline (an LLM call to suggest a case) can
    take tens of seconds to minutes depending on the AI backend's
    reachability/load, which this test shouldn't be at the mercy of.
    (test_case_graph.py / test_claim_status_transition.py apply the same
    principle: bypass the AI-dependent stages via direct seeding.)

    LIKE on the suffix, not an exact title match: extract_clean_title()
    (service.py) only normalizes the raw filename into the cleaned title
    once the extract stage actually runs, and whether that's synchronous
    with the /upload response isn't guaranteed even under
    CELERY_TASK_ALWAYS_EAGER — LIKE matches either form.
    """
    cur = conn.cursor()
    cur.execute(
        "UPDATE documents SET pipeline_state = 'completed' WHERE title LIKE ?",
        (f"%{suffix}%",),
    )
    assert cur.rowcount == 1, (
        f"Expected to fast-forward exactly one doc, got {cur.rowcount}"
    )
    conn.commit()


def test_triage_confirm_routes_doc_to_case(page: Page, api_client, db_seed):
    """Upload → triage → Route → pick case in modal → row gone, doc on case."""
    conn, _cleanup = db_seed
    case_id, suffix = _seed_doc_and_case(api_client, db_seed)

    page.goto("/triage")
    expect(page.locator(f"text=e2e-confirm-{suffix}")).to_be_visible(timeout=10_000)

    row = (
        page.locator("[data-bundle-key]").filter(has_text=f"e2e-confirm-{suffix}").first
    )

    # A generic test doc's content won't semantically match any existing
    # case, so triage_row.html renders "Route" (action: assign_case) rather
    # than "Confirm bundle" (action: confirm_bundle, which only renders when
    # lead_sub.suggested_case_id is set by the AI classifier). Both dispatch
    # the same triage:open-bundle-confirm modal.
    #
    # The extract->metadata pipeline is still dispatched somewhat
    # asynchronously even under CELERY_TASK_ALWAYS_EAGER (observed ~1s+
    # lag locally), so a background task can race _seed_doc_and_case's
    # fast-forward and revert pipeline_state back to running/pending after
    # it already committed 'completed'. Re-apply the fast-forward on each
    # retry rather than just waiting — a fixed budget fighting a fast local
    # DB race, not the tens-of-seconds a real AI call would need.
    route_button = row.get_by_role("button", name="Route")
    for _ in range(10):
        if route_button.is_visible():
            break
        _force_pipeline_completed(conn, suffix)
        page.wait_for_timeout(1_000)
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

    # The row should leave the queue. A page-wide text= locator isn't
    # specific enough here: triage_row.html renders the row's expanded
    # content (id="triage-row-expanded-{key}") as a SEPARATE sibling
    # element that also contains the doc's filename and is only
    # display:none'd, not removed — so a generic text search keeps
    # matching it even after the actual row is gone. Scope to the row
    # element itself, matching how it was located above.
    expect(
        page.locator("[data-bundle-key]").filter(has_text=f"e2e-confirm-{suffix}")
    ).to_have_count(0, timeout=15_000)

    # Verify the doc landed on the target case via the API.
    case_page = api_client.get(f"/cases/{case_id}")
    assert case_page.status_code == 200
    assert f"e2e-confirm-{suffix}" in case_page.text, (
        f"Doc not visible on case {case_id}'s page after confirm"
    )
