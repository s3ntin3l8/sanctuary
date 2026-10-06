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

FIXED (Issue #97): `POST /triage/confirm` used to intermittently 500 under
concurrent CELERY_TASK_ALWAYS_EAGER load — NOT because of AI-backend
reachability, despite that being the issue's original (incorrect) diagnosis.
`_summarize_document_sync` (ai_summary.py) is not on this call path at all;
its httpx.ConnectError traceback in the original CI logs was unrelated
background upload-pipeline noise from a concurrent doc.

The confirmed vulnerable code path: confirm_bundle's/confirm_document's own
`db.commit()` in triage_confirmation.py, contended by concurrent EAGER
background pipeline threads writing the same tables, can raise
`sqlalchemy.exc.OperationalError: database is locked` — an unhandled
exception that both 500'd the request and rolled back the case_id cascade
(confirmed via a deterministic local repro forcing that exact commit to hit
the lock). CI's original failures completed the whole 3-test e2e suite in
~25s total, which rules out a full 60s `busy_timeout` wait on any single
test — so in CI this was almost certainly the fast-failing case (a WAL
snapshot-upgrade conflict, i.e. `database is locked` returned immediately
because a stale read snapshot can't promote to a writer once a concurrent
connection has committed), not a sustained lock hold running out the clock.
Either sub-case is the same exception from the same call site, and the fix
(below) handles both.

Fixed by wrapping the cascade's mutate+commit in `retry_on_db_locked`
(pipeline_status.py) — the existing codebase pattern for this exact class of
contention. Note the mutations had to move *inside* the retried closure:
retry_on_db_locked's own `db.rollback()` (on a caught lock error) expires
the session's pending attribute changes, so retrying a bare `db.commit()`
alone would silently no-op instead of re-applying the cascade.
"""

import uuid

import pytest
from playwright.sync_api import Page, expect

pytestmark = pytest.mark.e2e


def _seed_doc_and_case(api_client, db_seed) -> tuple[str, str]:
    """Create a target Case via the live API and a completed triage Document
    directly in the database.

    The document is seeded as a row, not uploaded: an upload dispatches the
    AI pipeline, whose background stages keep rewriting ``pipeline_state``
    (and in CI fail against an unreachable AI backend), so the row never
    settles into a state that offers "Route". Upload itself is covered by
    the API integration tests.
    """
    conn, cleanup = db_seed
    suffix = uuid.uuid4().hex[:6].upper()
    case_id = f"E2E-CONF-{suffix}"

    resp = api_client.post(
        "/api/v1/cases",
        json={
            "case_id": case_id,
            "title": f"E2E Confirm {suffix}",
            "court_name": "AG Hamburg",
        },
    )
    assert resp.status_code == 201, f"Case create failed: {resp.status_code}"

    cur = conn.cursor()
    # The triage feed is owner-scoped: seed the row for the signed-in user.
    owner_id = api_client.get("/api/v1/shell").json()["user"]["id"]
    batch_id = cur.execute(
        """
        INSERT INTO ingest_batches
            (owner_id, source_type, subject, status, received_at, ingest_date)
        VALUES (%s, 'MANUAL', %s, 'PROCESSING', now(), now())
        RETURNING id
        """,
        (owner_id, f"e2e-confirm-{suffix}.txt"),
    ).fetchone()[0]
    cur.execute(
        """
        INSERT INTO documents
            (title, owner_id, case_id, ingest_batch_id, originator_type, status,
             pipeline_state, page_count, role, court_relay, thread_open,
             needs_review, review_reasons, received_date, ingest_date)
        VALUES (%s, %s, '_TRIAGE', %s, 'UNKNOWN', 'ACTIVE', 'completed', 1,
                'STANDALONE', false, false, true, '["pending_confirmation"]',
                now(), now())
        """,
        (f"e2e-confirm-{suffix}", owner_id, batch_id),
    )
    conn.commit()

    def _cleanup():
        c = conn.cursor()
        c.execute("DELETE FROM documents WHERE ingest_batch_id = %s", (batch_id,))
        c.execute("DELETE FROM ingest_batches WHERE id = %s", (batch_id,))
        c.execute("DELETE FROM cases WHERE id = %s", (case_id,))
        conn.commit()

    cleanup.append(_cleanup)
    return case_id, suffix


def test_triage_confirm_routes_doc_to_case(page: Page, api_client, db_seed):
    """Upload → triage → Route → pick case in modal → case cascaded to doc."""
    conn, _cleanup = db_seed
    case_id, suffix = _seed_doc_and_case(api_client, db_seed)

    page.goto("/triage")
    expect(page.locator(f"text=e2e-confirm-{suffix}")).to_be_visible(timeout=10_000)

    row = (
        page.locator("[data-bundle-key]").filter(has_text=f"e2e-confirm-{suffix}").first
    )
    # No AI suggestion → the row offers "Route" (assign_case), not "Confirm".
    route_button = row.get_by_role("button", name="Route", exact=True)
    expect(route_button).to_be_visible(timeout=10_000)
    route_button.click()

    # The SPA confirm dialog: "Route" opens it without a suggestion, so the
    # case <select> (id=case_id) is rendered rather than the suggestion card.
    dialog = page.get_by_role("dialog")
    expect(dialog).to_be_visible(timeout=5_000)
    dialog.locator("select#case_id").select_option(value=case_id)
    submit_button = dialog.get_by_role("button", name="Assign")
    expect(submit_button).to_be_enabled(timeout=5_000)

    with page.expect_response("**/api/v1/triage/confirm") as response_info:
        submit_button.click()
    resp = response_info.value
    assert resp.status == 200, (
        f"POST /api/v1/triage/confirm returned {resp.status}: {resp.text()[:2000]!r}"
    )

    # confirm_bundle (service.py) with finalize=False — what action=assign_case
    # uses — cascades case_id onto every doc in the bundle but deliberately
    # calls compute_review_reasons(doc, confirmed=False), so needs_review
    # stays true and the bundle is NOT removed from triage. Only the other
    # action, confirm_bundle (finalize=True, "Confirm bundle" button, gated
    # on an AI suggestion), does that. So the row is expected to still be
    # present here, not gone.
    #
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
        "SELECT case_id FROM documents WHERE title LIKE %s", (f"%{suffix}%",)
    ).fetchone()
    assert row_case_id is not None, f"Seeded doc for suffix {suffix} not found"
    assert row_case_id[0] == case_id, (
        f"Doc's case_id is {row_case_id[0]!r}, expected {case_id!r} after confirm"
    )
