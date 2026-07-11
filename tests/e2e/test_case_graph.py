"""E2E: case dashboard renders the correspondence graph.

Journey: navigate to /cases/{id} → graph view is the default → SVG renders
with nodes for each document → switching proceeding/filter triggers a
partial swap and re-renders.

CLAUDE.md: "Graph first: primary case view is the correspondence swim-lane
graph, not a document list." This test pins that the graph DOM is present
and that nodes appear when documents exist.
"""

import uuid
from datetime import datetime

import pytest
from playwright.sync_api import Page, expect

pytestmark = pytest.mark.e2e


def _seed_case_with_two_docs(api_client, db_seed) -> tuple[str, list[int]]:
    """Create a Case + Proceeding + 2 Documents linked by REPLIES_TO.

    Returns (case_id, [doc_id_1, doc_id_2]).
    """
    conn, cleanup = db_seed
    suffix = uuid.uuid4().hex[:6].upper()
    case_id = f"E2E-GRAPH-{suffix}"

    resp = api_client.post(
        "/cases",
        data={
            "case_id": case_id,
            "title": f"E2E Graph {suffix}",
            "court_name": "AG Hamburg",
        },
        follow_redirects=False,
    )
    assert resp.status_code in (200, 303)

    proc_row = conn.execute(
        "SELECT id FROM proceedings WHERE case_id = ?", (case_id,)
    ).fetchone()
    assert proc_row, "Proceeding not auto-created"
    proceeding_id = proc_row[0]

    now = datetime.now().isoformat(sep=" ")
    cur = conn.cursor()
    # status is NOT NULL with only an ORM-side (Python) default — raw SQL
    # bypasses that, so it must be supplied explicitly here. Enum-backed
    # columns store the member NAME (uppercase), not StrEnum's lowercase
    # .value, EXCEPT pipeline_state, which opts into .value via
    # values_callable — see app/models/database.py.
    cur.execute(
        """INSERT INTO documents
           (title, case_id, proceeding_id, originator_type, role, ingest_date,
            needs_review, court_relay, thread_open, page_count, pipeline_state, status)
           VALUES (?, ?, ?, 'COURT', 'STANDALONE', ?, 0, 0, 0, 1, 'completed', 'ACTIVE')""",
        (f"Graph Doc A {suffix}", case_id, proceeding_id, now),
    )
    doc_a = cur.lastrowid
    cur.execute(
        """INSERT INTO documents
           (title, case_id, proceeding_id, originator_type, role, ingest_date,
            needs_review, court_relay, thread_open, page_count, pipeline_state, status)
           VALUES (?, ?, ?, 'OWN', 'STANDALONE', ?, 0, 0, 0, 1, 'completed', 'ACTIVE')""",
        (f"Graph Doc B {suffix}", case_id, proceeding_id, now),
    )
    doc_b = cur.lastrowid
    cur.execute(
        """INSERT INTO document_relationships
           (from_document_id, to_document_id, relationship_type, confidence, ingest_date)
           VALUES (?, ?, 'REPLIES_TO', 'AI_DETECTED', ?)""",
        (doc_b, doc_a, now),
    )
    conn.commit()

    def _cleanup(c):
        c.execute(
            "DELETE FROM document_relationships WHERE from_document_id IN (?, ?) OR to_document_id IN (?, ?)",
            (doc_a, doc_b, doc_a, doc_b),
        )
        c.execute("DELETE FROM documents WHERE id IN (?, ?)", (doc_a, doc_b))
        c.execute("DELETE FROM proceedings WHERE id = ?", (proceeding_id,))
        c.execute("DELETE FROM cases WHERE id = ?", (case_id,))

    cleanup.append(_cleanup)
    return case_id, [doc_a, doc_b]


def test_case_dashboard_renders_graph_svg_with_nodes(page: Page, api_client, db_seed):
    """Case dashboard's default view is the graph — SVG with one node per doc."""
    case_id, doc_ids = _seed_case_with_two_docs(api_client, db_seed)

    page.goto(f"/cases/{case_id}")

    # Graph container is present in the dashboard layout.
    graph = page.locator("#graph-container, .case-graph, svg").first
    expect(graph).to_be_visible(timeout=10_000)

    # Each seeded document should render as a node. correspondence_graph.html
    # sets data-id="{{ node.id }}" (node.id == doc.id) on rendered node
    # groups; if that attribute changes, this test will surface the drift.
    for doc_id in doc_ids:
        node = page.locator(f"[data-id='{doc_id}']").first
        expect(node).to_be_attached(timeout=5_000)
