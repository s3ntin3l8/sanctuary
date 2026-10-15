"""Read tools: timeline, Truth Map, graph, financials, action items, triage inbox."""

from datetime import UTC, datetime, timedelta

import pytest

from app.models.database import (
    ActionItem,
    Case,
    Claim,
    ClaimEvidence,
    Document,
    DocumentRelationship,
    IngestBatch,
    LegalCost,
    Proceeding,
)
from app.models.enums import (
    ActionItemStatus,
    ActionItemType,
    ClaimEvidenceRole,
    ClaimStatus,
    ClaimType,
    CostCategory,
    CostStatus,
    IngestBatchSourceType,
    IngestBatchStatus,
    OriginatorType,
    PipelineState,
    ProceedingCourtLevel,
    ProceedingStatus,
    RelationshipConfidence,
    RelationshipType,
    SignificanceTier,
)

NOW = datetime.now(UTC)


def _result(payload):
    return (
        payload["result"]
        if isinstance(payload, dict) and "result" in payload
        else payload
    )


@pytest.fixture
def world(db_session, alice, bob, make_case):
    """Alice's case A-1 (two docs, a claim, deadlines, a cost); Bob's case B-1."""
    make_case("A-1", alice.id)
    make_case("B-1", bob.id)
    proc = Proceeding(
        case_id="A-1",
        court_name="AG Hamburg",
        court_level=ProceedingCourtLevel.AG,
        az_court="3 F 1/26",
        status=ProceedingStatus.ACTIVE,
        ingest_date=NOW,
    )
    other = Proceeding(
        case_id="B-1",
        court_name="AG Bonn",
        court_level=ProceedingCourtLevel.AG,
        status=ProceedingStatus.ACTIVE,
        ingest_date=NOW,
    )
    db_session.add_all([proc, other])
    db_session.flush()

    def doc(title, case_id, owner_id, proceeding_id, originator, tier, day):
        d = Document(
            title=title,
            content="text",
            case_id=case_id,
            owner_id=owner_id,
            proceeding_id=proceeding_id,
            originator_type=originator,
            significance_tier=tier,
            issued_date=datetime(2026, 1, day, tzinfo=UTC),
            ingest_date=datetime(2026, 1, day, tzinfo=UTC),
        )
        db_session.add(d)
        db_session.flush()
        return d

    d1 = doc(
        "Klage",
        "A-1",
        alice.id,
        proc.id,
        OriginatorType.OPPOSING,
        SignificanceTier.CRITICAL,
        10,
    )
    d2 = doc(
        "Erwiderung",
        "A-1",
        alice.id,
        proc.id,
        OriginatorType.OWN,
        SignificanceTier.CRITICAL,
        20,
    )
    b1 = doc(
        "Bobs Brief",
        "B-1",
        bob.id,
        other.id,
        OriginatorType.OPPOSING,
        SignificanceTier.CRITICAL,
        5,
    )
    db_session.add(
        DocumentRelationship(
            from_document_id=d2.id,
            to_document_id=d1.id,
            relationship_type=RelationshipType.REPLIES_TO,
            confidence=RelationshipConfidence.USER_CREATED,
        )
    )
    claim = Claim(
        claim_text="Die Zahlung ist nicht erfolgt.",
        claim_type=ClaimType.FACTUAL,
        status=ClaimStatus.ASSERTED,
        first_made_at=NOW,
        last_updated_at=NOW,
    )
    db_session.add(claim)
    db_session.flush()
    db_session.add_all(
        [
            ClaimEvidence(
                claim_id=claim.id,
                document_id=d1.id,
                role=ClaimEvidenceRole.ASSERTS,
                excerpt="keine Zahlung",
            ),
            # Evidence from a case the token below cannot read.
            ClaimEvidence(
                claim_id=claim.id,
                document_id=b1.id,
                role=ClaimEvidenceRole.SUPPORTS,
                excerpt="geheim",
            ),
        ]
    )
    live = ActionItem(
        case_id="A-1",
        proceeding_id=proc.id,
        source_document_id=d1.id,
        title="Erwiderung einreichen",
        due_date=NOW + timedelta(days=5),
        action_type=ActionItemType.DEADLINE,
        status=ActionItemStatus.OPEN,
    )
    done = ActionItem(
        case_id="A-1",
        title="Termin wahrgenommen",
        due_date=NOW - timedelta(days=9),
        action_type=ActionItemType.COURT_DATE,
        status=ActionItemStatus.COMPLETED,
    )
    old = ActionItem(
        case_id="A-1",
        title="Alte Frist",
        due_date=NOW + timedelta(days=1),
        action_type=ActionItemType.DEADLINE,
        status=ActionItemStatus.OPEN,
        superseded=True,
    )
    db_session.add_all([live, done, old])
    db_session.add(
        LegalCost(
            case_id="A-1",
            proceeding_id=proc.id,
            category=CostCategory.ANWALTSKOSTEN,
            status=CostStatus.OFFEN,
            title="Vorschuss",
            amount_net=100.0,
            vat_rate=0.19,
            amount_gross=119.0,
        )
    )
    db_session.commit()
    return {"proc": proc, "other": other, "d1": d1, "d2": d2, "b1": b1, "claim": claim}


# --- timeline ----------------------------------------------------------------


def test_timeline_has_cited_document_events(mcp_session, make_token, alice, world):
    error, out = mcp_session(make_token(alice)).call("get_timeline", case_id="A-1")
    assert not error
    events = _result(out)
    titles = {e["title"] for e in events}
    assert {"Klage", "Erwiderung"} <= titles
    klage = next(e for e in events if e["title"] == "Klage")
    assert klage["document"]["document_id"] == world["d1"].id
    assert klage["document"]["url"] == f"/document/{world['d1'].id}"
    assert [e["date"] for e in events] == sorted(e["date"] for e in events)


def test_timeline_scope(mcp_session, make_token, alice, world):
    scoped = mcp_session(make_token(alice, all_cases=False, case_ids=["A-1"]))
    assert not scoped.call("get_timeline", case_id="A-1")[0]
    error, msg = scoped.call("get_timeline", case_id="B-1")
    assert error and "outside this token's scope" in msg


# --- truth map ---------------------------------------------------------------


def test_truth_map_lists_claims_with_evidence(mcp_session, make_token, alice, world):
    error, out = mcp_session(make_token(alice)).call("get_truth_map", case_id="A-1")
    assert not error
    assert out["open_claim_count"] == 1
    claim = out["claims"][0]
    assert claim["text"] == "Die Zahlung ist nicht erfolgt."
    assert claim["status"] == "asserted"
    asserts = [e for e in claim["evidence"] if e["role"] == "asserts"]
    assert asserts[0]["excerpt"] == "keine Zahlung"
    assert asserts[0]["document"]["document_id"] == world["d1"].id
    assert out["pending_merge_proposals"] == 0


def test_truth_map_hides_evidence_from_unreadable_cases(
    mcp_session, make_token, alice, bob, world, db_session
):
    # Alice can see Bob's case through a share only in the REST layer; here the
    # evidence document belongs to a case she has no access to at all.
    error, out = mcp_session(make_token(alice)).call("get_truth_map", case_id="A-1")
    assert not error
    docs = {e["document"]["document_id"] for e in out["claims"][0]["evidence"]}
    assert world["b1"].id not in docs


def test_truth_map_status_filter(mcp_session, make_token, alice, world):
    session = mcp_session(make_token(alice))
    assert (
        session.call("get_truth_map", case_id="A-1", status="established")[1]["claims"]
        == []
    )
    assert (
        len(session.call("get_truth_map", case_id="A-1", status="all")[1]["claims"])
        == 1
    )


def test_truth_map_scope(mcp_session, make_token, alice, world):
    scoped = mcp_session(make_token(alice, all_cases=False, case_ids=["A-1"]))
    assert scoped.call("get_truth_map", case_id="B-1")[0]


# --- graph -------------------------------------------------------------------


def test_graph_has_nodes_and_typed_edges(mcp_session, make_token, alice, world):
    error, out = mcp_session(make_token(alice)).call(
        "get_case_graph", proceeding_id=world["proc"].id, filter="all"
    )
    assert not error
    ids = {n["document"]["document_id"] for n in out["nodes"]}
    assert ids == {world["d1"].id, world["d2"].id}
    assert {n["lane"] for n in out["nodes"]} <= {
        "own",
        "court",
        "opposing",
        "third",
        "unknown",
    }
    assert out["node_counts"]
    assert [e["kind"] for e in out["edges"]] == ["reply"]


def test_graph_of_another_users_proceeding_is_not_found(
    mcp_session, make_token, alice, world
):
    session = mcp_session(make_token(alice))
    error, msg = session.call("get_case_graph", proceeding_id=world["other"].id)
    assert error and "outside this token's scope" in msg
    assert session.call("get_case_graph", proceeding_id=999999)[0]


def test_graph_per_case_token_scope(mcp_session, make_token, alice, world, make_case):
    make_case("A-2", alice.id)
    scoped = mcp_session(make_token(alice, all_cases=False, case_ids=["A-2"]))
    assert scoped.call("get_case_graph", proceeding_id=world["proc"].id)[0]


# --- financials --------------------------------------------------------------


def test_financials_report_recorded_costs(mcp_session, make_token, alice, world):
    error, out = mcp_session(make_token(alice)).call("get_financials", case_id="A-1")
    assert not error
    assert {"summary", "instances", "case_level_costs", "signal_docs"} <= out.keys()
    assert out["instances"][0]["az_court"] == "3 F 1/26"
    own = out["instances"][0]["invoices_own"] + out["instances"][0]["invoices_other"]
    assert any(c["title"] == "Vorschuss" and c["amount_gross"] == 119.0 for c in own)


def test_financials_scope(mcp_session, make_token, alice, world):
    scoped = mcp_session(make_token(alice, all_cases=False, case_ids=["A-1"]))
    assert scoped.call("get_financials", case_id="B-1")[0]


# --- action items ------------------------------------------------------------


def test_action_items_default_to_open_and_hide_superseded(
    mcp_session, make_token, alice, world
):
    session = mcp_session(make_token(alice))
    items = _result(session.call("list_action_items", case_id="A-1")[1])
    assert [i["title"] for i in items] == ["Erwiderung einreichen"]
    assert items[0]["source_document"]["document_id"] == world["d1"].id
    assert items[0]["is_overdue"] is False

    everything = _result(
        session.call("list_action_items", case_id="A-1", include_done=True)[1]
    )
    assert {i["title"] for i in everything} == {
        "Erwiderung einreichen",
        "Termin wahrgenommen",
    }


def test_action_items_scope(mcp_session, make_token, alice, world):
    scoped = mcp_session(make_token(alice, all_cases=False, case_ids=["A-1"]))
    assert scoped.call("list_action_items", case_id="B-1")[0]


# --- triage inbox ------------------------------------------------------------


def _inbox_batch(db, owner_id, subject):
    batch = IngestBatch(
        owner_id=owner_id,
        source_type=IngestBatchSourceType.EMAIL,
        subject=subject,
        sender_email="kanzlei@example.com",
        status=IngestBatchStatus.OPEN,
    )
    db.add(batch)
    db.flush()
    db.add(
        Document(
            title=f"{subject} 1",
            owner_id=owner_id,
            case_id="_TRIAGE",
            ingest_batch_id=batch.id,
            pipeline_state=PipelineState.COMPLETED,
            page_count=2,
        )
    )
    db.commit()


def test_triage_inbox_needs_an_all_cases_token(
    mcp_session, make_token, alice, world, db_session
):
    _inbox_batch(db_session, alice.id, "Post")
    scoped = mcp_session(make_token(alice, all_cases=False, case_ids=["A-1"]))
    error, msg = scoped.call("list_triage_inbox")
    assert error and "all-cases token" in msg


def test_triage_inbox_shows_only_the_callers_own_bundles(
    mcp_session, make_token, alice, bob, db_session
):
    _inbox_batch(db_session, alice.id, "Alices Post")
    _inbox_batch(db_session, bob.id, "Bobs Post")
    error, out = mcp_session(make_token(alice)).call("list_triage_inbox")
    assert not error
    bundles = _result(out)
    assert [b["subject"] for b in bundles] == ["Alices Post"]
    assert bundles[0]["documents"][0]["document"]["title"] == "Alices Post 1"


@pytest.mark.parametrize("state", ["suggested", "draft", "confirmed"])
def test_triage_inbox_names_the_case_in_every_state(
    state, mcp_session, make_token, alice, world, db_session
):
    """case_title is filled whichever way the bundle points at its case, and a
    bundle never carries a confirmed and a suggested case at once."""
    _inbox_batch(db_session, alice.id, "Post")
    batch = db_session.query(IngestBatch).filter_by(subject="Post").one()
    case = db_session.get(Case, "A-1")
    if state == "suggested":  # the pipeline filed a document, nobody confirmed it
        db_session.query(Document).filter_by(ingest_batch_id=batch.id).update(
            {"case_id": "A-1"}
        )
    else:
        batch.case_id = "A-1"
        case.is_draft = state == "draft"
    db_session.commit()
    error, out = mcp_session(make_token(alice)).call("list_triage_inbox")
    assert not error
    (bundle,) = [b for b in _result(out) if b["subject"] == "Post"]
    assert bundle["case_title"] == case.title
    expected_suggested = "A-1" if state in ("suggested", "draft") else None
    assert bundle["suggested_case_id"] == expected_suggested
    assert bundle["confirmed_case_id"] == (None if expected_suggested else "A-1")
