"""E2E: triage confirm flow.

Journey: a doc lands in the Triage Inbox with no AI-suggested case → user
clicks "Route" → picks a target case in the modal → the case is cascaded
onto the document and it appears under that case's page.

Per document_ops.py's `confirm` route docstring, action=assign_case
(what "Route" dispatches) "cascade[s] case_id, batch stays in triage" —
by design, the bundle is NOT removed from the triage queue by this action
(only action=confirm_bundle, gated on an AI-suggested case, does that).
This test verifies the actual documented behavior: the case assignment
lands, not that the row disappears.

This pins the highest-frequency happy-path workflow described in
CLAUDE.md (`Triage is a strategy session`). Drives the unified
`/triage/confirm` endpoint via the UI.
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
    """Upload → triage → Route → pick case in modal → case cascaded to doc."""
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

    # The submit button is :disabled="!isNewCase && !bundleConfirm.suggested_case_id"
    # (triage_bundle_confirm_modal.html) — the <select> has
    # x-model="bundleConfirm.suggested_case_id", so selecting an option
    # should clear that disabled state via Alpine's own reactivity. Assert
    # it explicitly rather than assuming: if this fails, the problem is
    # client-side reactivity, not the server; if it passes but the DB
    # assertion below still fails, the problem is server-side.
    submit_button = form.locator("button[type='submit']")
    expect(submit_button).to_be_enabled(timeout=5_000)

    # Capture whether the POST actually fires and with what payload —
    # settles client-vs-server ambiguity in one run instead of more
    # guessing if the DB assertion below still fails.
    with page.expect_request("**/triage/confirm") as request_info:
        submit_button.click()
    sent_post_data = request_info.value.post_data
    assert sent_post_data and f"case_id={case_id}" in sent_post_data, (
        f"POST /triage/confirm didn't include the selected case_id; "
        f"sent: {sent_post_data!r}"
    )

    # confirm_bundle (service.py) with finalize=False — what action=assign_case
    # uses — cascades case_id onto every doc in the bundle but deliberately
    # calls compute_review_reasons(doc, confirmed=False), so needs_review
    # stays true and the bundle is NOT removed from triage. Only the other
    # action, confirm_bundle (finalize=True, "Confirm bundle" button, gated
    # on an AI suggestion), does that. So the row is expected to still be
    # present here, not gone.
    #
    # The row's own case chip (triage_row.html's "unassigned" / "no
    # suggestion" text) reflects bundle-level confirmed_case_id /
    # suggested_case_id — neither of which assign_case touches (it only
    # cascades case_id onto the individual Document rows, not any
    # bundle-level aggregate field). Confirmed against an actual failing
    # run's rendered row: it still read "unassigned" after a successful
    # assign_case. So the row staying on "unassigned" here is correct,
    # expected UI behavior, not a sign anything failed — the real check is
    # the DB-level case cascade below.
    row = (
        page.locator("[data-bundle-key]").filter(has_text=f"e2e-confirm-{suffix}").first
    )
    expect(row).to_be_visible(timeout=15_000)

    # Verify the case cascade landed, via a direct DB check rather than the
    # case page's rendered HTML: the case dashboard's default (and only
    # server-rendered-text-searchable) view is the correspondence graph
    # (CLAUDE.md: "Graph first"), which doesn't include the plain doc title
    # as literal page text — confirmed by inspecting an actual failing run's
    # response body, not assumed.
    row_case_id = conn.execute(
        "SELECT case_id FROM documents WHERE title LIKE ?", (f"%{suffix}%",)
    ).fetchone()
    assert row_case_id is not None, f"Seeded doc for suffix {suffix} not found"
    assert row_case_id[0] == case_id, (
        f"Doc's case_id is {row_case_id[0]!r}, expected {case_id!r} after confirm"
    )
