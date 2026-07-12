"""E2E: claim status transitions in the truth map.

Journey: open the truth map for a case → click an ASSERTED claim's status
pill → choose "Mark Established" → claim card swaps in place → claim
appears under the Established group on next reload.

CLAUDE.md: "Three layers: Structural → Factual → Strategic." The Truth
Map is the Factual layer's primary surface; the user must be able to
move a claim through ASSERTED → ESTABLISHED → CONTESTED states without
a page reload.
"""

import uuid
from datetime import UTC, datetime

import pytest
from playwright.sync_api import Page, expect

pytestmark = pytest.mark.e2e


def _seed_case_with_claim(api_client, db_seed) -> tuple[str, int]:
    """Create a Case + source Document + one ASSERTED Claim. Returns (case_id, claim_id)."""
    conn, cleanup = db_seed
    suffix = uuid.uuid4().hex[:6].upper()
    case_id = f"E2E-CLAIM-{suffix}"

    resp = api_client.post(
        "/cases",
        data={
            "case_id": case_id,
            "title": f"E2E Claim {suffix}",
            "court_name": "AG Hamburg",
        },
        follow_redirects=False,
    )
    assert resp.status_code in (200, 303)

    now = datetime.now(UTC)
    cur = conn.cursor()
    # status is NOT NULL with only an ORM-side (Python) default — raw SQL
    # bypasses that, so it must be supplied explicitly here. Enum-backed
    # columns store the member NAME (uppercase), not StrEnum's lowercase
    # .value, EXCEPT pipeline_state, which opts into .value via
    # values_callable — see app/models/database.py.
    cur.execute(
        """INSERT INTO documents
           (title, case_id, originator_type, role, ingest_date,
            needs_review, court_relay, thread_open, page_count, pipeline_state, status)
           VALUES (%s, %s, 'OWN', 'STANDALONE', %s, false, false, false, 1, 'completed', 'ACTIVE')
           RETURNING id""",
        (f"Claim Source {suffix}", case_id, now),
    )
    source_doc_id = cur.fetchone()[0]
    # claims has no case_id/source_document_id column — Wave 2A
    # (d2c4f9a1b6e8_drop_claim_case_columns) made claims global/cross-case.
    # Case context lives entirely on ClaimEvidence: claims_for_case() (see
    # app/repositories/claim.py) joins ClaimEvidence -> Document.case_id, and
    # a ClaimEvidence(role=ASSERTS) row is the canonical "originated by" link
    # (claims_asserted_by_document()).
    cur.execute(
        """INSERT INTO claims
           (claim_text, claim_type, status, is_precedent, first_made_at, last_updated_at)
           VALUES (%s, 'FACTUAL', 'ASSERTED', false, %s, %s)
           RETURNING id""",
        (f"Test claim {suffix}", now, now),
    )
    claim_id = cur.fetchone()[0]
    cur.execute(
        """INSERT INTO claim_evidence
           (claim_id, document_id, role, confidence, ingest_date)
           VALUES (%s, %s, 'ASSERTS', 'AI_DETECTED', %s)""",
        (claim_id, source_doc_id, now),
    )
    conn.commit()

    proc_row = conn.execute(
        "SELECT id FROM proceedings WHERE case_id = %s", (case_id,)
    ).fetchone()
    proceeding_id = proc_row[0] if proc_row else None

    def _cleanup(c):
        c.execute("DELETE FROM claim_evidence WHERE claim_id = %s", (claim_id,))
        c.execute("DELETE FROM claims WHERE id = %s", (claim_id,))
        c.execute("DELETE FROM documents WHERE id = %s", (source_doc_id,))
        if proceeding_id:
            c.execute("DELETE FROM proceedings WHERE id = %s", (proceeding_id,))
        c.execute("DELETE FROM cases WHERE id = %s", (case_id,))

    cleanup.append(_cleanup)
    return case_id, claim_id


def test_asserted_claim_can_be_marked_established(page: Page, api_client, db_seed):
    """ASSERTED → ESTABLISHED transition swaps the claim card in place."""
    case_id, claim_id = _seed_case_with_claim(api_client, db_seed)

    # dashboard.js reads ?view= directly into Alpine state with no mapping;
    # the truth-map pane's x-show checks view === 'truth' (not 'truthmap').
    page.goto(f"/cases/{case_id}?view=truth")

    card = page.locator(f"#claim-card-{claim_id}")
    expect(card).to_be_visible(timeout=10_000)

    # The status pill is a button. Click it to open the dropdown, then pick
    # "Mark Established". The card swaps via HTMX outerHTML.
    status_pill = card.get_by_role("button").first
    status_pill.click()
    page.get_by_role("button", name="Mark Established").first.click()

    # After swap, the new card's pill text should read "Established".
    new_card = page.locator(f"#claim-card-{claim_id}")
    expect(new_card.get_by_text("Established", exact=False)).to_be_visible(
        timeout=5_000
    )
