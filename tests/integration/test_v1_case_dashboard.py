"""The case dashboard on /api/v1: detail, graph, timeline, truth map, claims,
financials, proceedings and sharing."""

from datetime import UTC, datetime, timedelta
from unittest.mock import patch

import pytest
from fastapi.testclient import TestClient

from app.main import app
from app.models.database import (
    ActionItem,
    Case,
    Claim,
    ClaimEvidence,
    CostSignal,
    Document,
    LegalCost,
    Proceeding,
    User,
)
from app.models.enums import (
    ActionItemStatus,
    ActionItemType,
    CaseStatus,
    ClaimEvidenceRole,
    ClaimStatus,
    ClaimType,
    CostCategory,
    CostSignalType,
    CostStatus,
    DocumentRole,
    Jurisdiction,
    OriginatorType,
    ProceedingCourtLevel,
    ProceedingStatus,
    SignificanceTier,
)

client = TestClient(app)


def _admin(db):
    return db.query(User).filter_by(email="admin@localhost").one()


@pytest.fixture
def dash(db_session):
    """A case with two proceedings, two docs, an action item, a cost and a claim."""
    admin = _admin(db_session)
    case = Case(
        id="DASH-001",
        title="Dashboard Case",
        status=CaseStatus.DISCOVERY,
        jurisdiction=Jurisdiction.DE,
        owner_id=admin.id,
        parties=[{"name": "Vane", "role": "own", "document_count": 1}],
    )
    db_session.add(case)
    db_session.flush()
    p1 = Proceeding(
        case_id=case.id,
        court_name="AG Hamburg",
        court_level=ProceedingCourtLevel.AG,
        az_court="3 F 1/26",
        status=ProceedingStatus.ACTIVE,
        ingest_date=datetime(2026, 1, 1, tzinfo=UTC),
    )
    p2 = Proceeding(
        case_id=case.id,
        court_name="OLG Hamburg",
        court_level=ProceedingCourtLevel.OLG,
        status=ProceedingStatus.ACTIVE,
        ingest_date=datetime(2026, 2, 1, tzinfo=UTC),
    )
    db_session.add_all([p1, p2])
    db_session.flush()
    d1 = Document(
        title="Klageschrift",
        content="Die Klage wird erhoben.",
        case_id=case.id,
        proceeding_id=p1.id,
        owner_id=admin.id,
        originator_type=OriginatorType.OPPOSING,
        role=DocumentRole.STANDALONE,
        significance_tier=SignificanceTier.CRITICAL,
        issued_date=datetime(2026, 1, 10, tzinfo=UTC),
        ingest_date=datetime(2026, 1, 11, tzinfo=UTC),
    )
    d2 = Document(
        title="Erwiderung",
        content="Wir erwidern.",
        case_id=case.id,
        proceeding_id=p1.id,
        owner_id=admin.id,
        originator_type=OriginatorType.OWN,
        role=DocumentRole.STANDALONE,
        significance_tier=SignificanceTier.ADMINISTRATIVE,
        issued_date=datetime(2026, 1, 20, tzinfo=UTC),
        ingest_date=datetime(2026, 1, 21, tzinfo=UTC),
    )
    db_session.add_all([d1, d2])
    db_session.flush()
    db_session.add(
        ActionItem(
            case_id=case.id,
            proceeding_id=p1.id,
            source_document_id=d1.id,
            title="Erwiderung einreichen",
            due_date=datetime.now(UTC) - timedelta(days=2),
            action_type=ActionItemType.DEADLINE,
            status=ActionItemStatus.OPEN,
        )
    )
    cost = LegalCost(
        case_id=case.id,
        proceeding_id=p1.id,
        category=CostCategory.ANWALTSKOSTEN,
        status=CostStatus.OFFEN,
        title="Vorschuss",
        amount_net=100.0,
        vat_rate=0.19,
        amount_gross=119.0,
    )
    db_session.add(cost)
    db_session.add(
        CostSignal(
            case_id=case.id,
            proceeding_id=p1.id,
            source_document_id=d1.id,
            signal_type=CostSignalType.COST_RULING,
            amount=5000.0,
            allocation={},
        )
    )
    claim = Claim(
        claim_text="Der Vater zahlt nicht.",
        claim_type=ClaimType.FACTUAL,
        status=ClaimStatus.ASSERTED,
        first_made_at=datetime.now(UTC),
        last_updated_at=datetime.now(UTC),
    )
    db_session.add(claim)
    db_session.flush()
    db_session.add(
        ClaimEvidence(
            claim_id=claim.id, document_id=d1.id, role=ClaimEvidenceRole.ASSERTS
        )
    )
    db_session.commit()
    return {
        "case": case,
        "p1": p1,
        "p2": p2,
        "d1": d1,
        "d2": d2,
        "cost": cost,
        "claim": claim,
    }


@pytest.mark.integration
def test_detail_shape_and_active_proceeding(db_session, dash):
    resp = client.get("/api/v1/cases/DASH-001")
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["id"] == "DASH-001"
    assert body["can_edit"] is True and body["can_manage_sharing"] is True
    assert [p["court_name"] for p in body["proceedings"]] == [
        "AG Hamburg",
        "OLG Hamburg",
    ]
    assert body["proceedings"][1]["is_deletable"] is True
    assert body["proceedings"][0]["is_deletable"] is False
    assert body["active_proceeding_id"] == dash["p1"].id
    assert [d["title"] for d in body["documents"]] == ["Erwiderung", "Klageschrift"]
    assert body["action_items"][0]["is_overdue"] is True
    assert body["open_claim_count"] == 1
    assert body["brief"]["status"] == "none"
    assert body["financials"]["booked"] == 119.0
    assert body["parties"] == [{"name": "Vane", "role": "own", "document_count": 1}]

    # Switching the proceeding is remembered and scopes the spine.
    resp = client.get(f"/api/v1/cases/DASH-001?proceeding={dash['p2'].id}")
    assert resp.json()["active_proceeding_id"] == dash["p2"].id
    assert resp.json()["documents"] == []
    assert (
        client.get("/api/v1/cases/DASH-001").json()["active_proceeding_id"]
        == dash["p2"].id
    )


@pytest.mark.integration
def test_visit_is_recorded_explicitly_and_feeds_new_markers(db_session, dash):
    """GET is side-effect free; POST /viewed records the visit; the graph marks
    documents newer than the `since` the client got from the detail call."""
    assert client.get("/api/v1/cases/DASH-001").json()["last_visit"] is None
    assert client.get("/api/v1/cases/DASH-001").json()["last_visit"] is None
    assert client.post("/api/v1/cases/DASH-001/viewed").status_code == 204
    doc = Document(
        title="Neu",
        case_id="DASH-001",
        proceeding_id=dash["p1"].id,
        owner_id=_admin(db_session).id,
        originator_type=OriginatorType.COURT,
        significance_tier=SignificanceTier.SIGNIFICANT,
        ingest_date=datetime.now(UTC) + timedelta(seconds=5),
    )
    db_session.add(doc)
    db_session.commit()
    detail = client.get("/api/v1/cases/DASH-001").json()
    assert detail["last_visit"] is not None
    assert detail["new_doc_count"] == 1
    assert next(d for d in detail["documents"] if d["title"] == "Neu")["is_new"] is True
    # Refetching the detail does not move the marker.
    assert client.get("/api/v1/cases/DASH-001").json()["new_doc_count"] == 1
    graph = client.get(
        f"/api/v1/cases/DASH-001/graph?proceeding={dash['p1'].id}&since={detail['last_visit']}"
    ).json()
    assert {n["id"] for n in graph["nodes"] if n["is_new_since_last_visit"]} == {doc.id}


@pytest.mark.integration
def test_foreign_proceeding_param_is_ignored(db_session, dash):
    other = Case(
        id="DASH-OTHER",
        title="Other",
        status=CaseStatus.INTAKE,
        jurisdiction=Jurisdiction.DE,
    )
    db_session.add(other)
    db_session.flush()
    foreign = Proceeding(
        case_id=other.id,
        court_name="LG Berlin",
        court_level=ProceedingCourtLevel.LG,
        status=ProceedingStatus.ACTIVE,
        ingest_date=datetime(2026, 3, 1, tzinfo=UTC),
    )
    db_session.add(foreign)
    db_session.commit()
    resp = client.get(f"/api/v1/cases/DASH-001?proceeding={foreign.id}")
    assert resp.json()["active_proceeding_id"] == dash["p1"].id


@pytest.mark.integration
def test_graph_and_timeline(db_session, dash):
    g = client.get(f"/api/v1/cases/DASH-001/graph?proceeding={dash['p1'].id}")
    assert g.status_code == 200, g.text
    body = g.json()
    assert body["filter"] == "significant+"
    assert [n["id"] for n in body["nodes"]] == [dash["d1"].id]  # administrative hidden
    assert body["node_counts"]["administrative_standalone"] == 1
    everything = client.get(
        f"/api/v1/cases/DASH-001/graph?proceeding={dash['p1'].id}&filter=all"
    ).json()
    assert len(everything["nodes"]) == 2
    assert (
        client.get("/api/v1/cases/DASH-001/graph?proceeding=999999").status_code == 404
    )

    t = client.get("/api/v1/cases/DASH-001/timeline")
    assert t.status_code == 200, t.text
    kinds = {e["id"].split("-")[0] for e in t.json()["events"]}
    assert {"doc", "action"} <= kinds
    assert t.json()["total_count"] == len(t.json()["events"])


@pytest.mark.integration
def test_truth_map_and_claim_transitions(db_session, dash):
    tm = client.get("/api/v1/cases/DASH-001/truthmap")
    assert tm.status_code == 200, tm.text
    body = tm.json()
    assert body["open_claim_count"] == 1
    claim = body["groups"][0]["claims"][0]
    assert claim["status"] == "asserted"
    assert set(claim["allowed_transitions"]) == {
        "established",
        "contested",
        "needs_proof",
    }
    assert claim["evidence"][0]["document_title"] == "Klageschrift"

    cid = dash["claim"].id
    moved = client.put(f"/api/v1/claims/{cid}/status", json={"status": "established"})
    assert moved.status_code == 200, moved.text
    assert moved.json()["status"] == "established"
    refused = client.put(f"/api/v1/claims/{cid}/status", json={"status": "refuted"})
    assert refused.status_code == 422
    assert refused.json()["code"] == "bad_transition"
    assert client.post(f"/api/v1/claims/{cid}/precedent").json()["is_precedent"] is True
    assert client.delete(f"/api/v1/claims/{cid}").status_code == 204
    assert (
        client.get("/api/v1/cases/DASH-001/truthmap?filter=all").json()["groups"] == []
    )


@pytest.mark.integration
def test_find_duplicates_starts_a_job(db_session, dash):
    with patch("app.api.v1.claims.dispatch_task", create=True) as dispatch:
        with patch("app.tasks.dispatch.dispatch_task") as real_dispatch:
            resp = client.post("/api/v1/cases/DASH-001/claims/find-duplicates")
    assert resp.status_code == 200, resp.text
    assert resp.json()["status"] == "running"
    assert resp.json()["total"] == 1
    assert real_dispatch.called or dispatch.called
    assert (
        client.get("/api/v1/cases/DASH-001/truthmap").json()["dedup_job"]["status"]
        == "running"
    )


@pytest.mark.integration
def test_financials_and_cost_mutations(db_session, dash):
    fin = client.get("/api/v1/cases/DASH-001/financials")
    assert fin.status_code == 200, fin.text
    body = fin.json()
    assert body["summary"]["booked"] == 119.0
    assert body["instances"][0]["court_name"] == "AG Hamburg"
    assert body["instances"][0]["invoices_own"][0]["title"] == "Vorschuss"
    assert body["signal_docs"][0]["signal_type"] == "cost_ruling"
    assert body["signal_docs"][0]["client_role"] is None

    cid = dash["cost"].id
    paid = client.post(f"/api/v1/costs/{cid}/pay")
    assert paid.status_code == 200, paid.text
    assert paid.json()["status"] == "bezahlt"
    edited = client.patch(
        f"/api/v1/costs/{cid}", json={"title": "Vorschuss 2", "amount_net": 200}
    )
    assert edited.json()["amount_gross"] == pytest.approx(238.0)
    assert client.patch(f"/api/v1/costs/{cid}", json={}).status_code == 422

    sid = body["signal_docs"][0]["signal_id"]
    assert (
        client.put(
            f"/api/v1/cost-signals/{sid}/client-role", json={"role": "winner"}
        ).status_code
        == 204
    )
    assert (
        client.get("/api/v1/cases/DASH-001/financials").json()["signal_docs"][0][
            "client_role"
        ]
        == "winner"
    )


@pytest.mark.integration
def test_case_and_proceeding_mutations(db_session, dash):
    upd = client.patch(
        "/api/v1/cases/DASH-001", json={"title": " Renamed ", "assume_worst_case": True}
    )
    assert upd.status_code == 200, upd.text
    assert upd.json()["title"] == "Renamed" and upd.json()["assume_worst_case"] is True
    assert (
        client.patch("/api/v1/cases/DASH-001", json={"title": "  "}).status_code == 422
    )

    parties = client.put(
        "/api/v1/cases/DASH-001/opposing-parties",
        json={"opposing_parties": [" Müller ", ""]},
    )
    assert parties.status_code == 200, parties.text
    db_session.expire_all()
    assert db_session.get(Case, "DASH-001").opposing_parties == ["Müller"]

    brief = client.get("/api/v1/cases/DASH-001/brief").json()
    assert brief["status"] == "none"
    with patch("app.tasks.dispatch.dispatch_task"):
        assert (
            client.post("/api/v1/cases/DASH-001/brief/refresh").json()["status"]
            == "processing"
        )

    p2 = dash["p2"].id
    renamed = client.patch(f"/api/v1/proceedings/{p2}", json={"az_court": "7 UF 9/26"})
    assert renamed.status_code == 200 and renamed.json()["az_court"] == "7 UF 9/26"
    assert client.delete(f"/api/v1/proceedings/{p2}").status_code == 204
    not_empty = client.delete(f"/api/v1/proceedings/{dash['p1'].id}")
    assert not_empty.status_code == 409

    closed = client.patch("/api/v1/cases/DASH-001", json={"status": "closed"})
    assert closed.json()["status"] == "closed"
    db_session.expire_all()
    assert db_session.get(Case, "DASH-001").closed_at is not None
    assert db_session.get(Proceeding, dash["p1"].id).status == ProceedingStatus.CLOSED


@pytest.mark.integration
def test_purge_needs_exact_confirmation(db_session, dash):
    assert (
        client.post(
            "/api/v1/cases/DASH-001/purge", json={"confirm": "nope"}
        ).status_code
        == 422
    )
    with patch(
        "app.services.case_service.CaseService.purge", return_value={"ok": True}
    ) as purge:
        assert (
            client.post(
                "/api/v1/cases/DASH-001/purge", json={"confirm": "purge DASH-001"}
            ).status_code
            == 204
        )
    purge.assert_called_once_with("DASH-001")


@pytest.mark.integration
def test_sharing_roundtrip(db_session, dash):
    from app.services import auth_service

    other = auth_service.create_user(
        db_session,
        email="viewer@example.com",
        password="password123",  # pragma: allowlist secret
    )
    db_session.commit()
    assert client.get("/api/v1/cases/DASH-001/shares").json()["shares"] == []
    added = client.post(
        "/api/v1/cases/DASH-001/shares",
        json={"email": "viewer@example.com", "permission": "viewer"},
    )
    assert added.status_code == 200, added.text
    assert added.json()["shares"][0]["user_id"] == other.id
    assert (
        client.post(
            "/api/v1/cases/DASH-001/shares",
            json={"email": "nobody@example.com", "permission": "viewer"},
        ).status_code
        == 404
    )
    assert (
        client.delete(f"/api/v1/cases/DASH-001/shares/{other.id}").json()["shares"]
        == []
    )


@pytest.mark.parametrize(
    "method, path, body",
    [
        ("patch", "/api/v1/cases/{case}", {"title": "x"}),
        ("post", "/api/v1/cases/{case}/purge", {"confirm": "purge {case}"}),
        ("put", "/api/v1/cases/{case}/opposing-parties", {"opposing_parties": []}),
        ("post", "/api/v1/cases/{case}/reenrich", None),
        ("post", "/api/v1/cases/{case}/brief/refresh", None),
        ("post", "/api/v1/cases/{case}/claims/find-duplicates", None),
        ("post", "/api/v1/cases/{case}/claims/proposals/merge", {"action": "confirm"}),
        ("put", "/api/v1/claims/{claim}/status", {"status": "established"}),
        ("post", "/api/v1/claims/{claim}/precedent", None),
        ("delete", "/api/v1/claims/{claim}", None),
        ("post", "/api/v1/costs/{cost}/pay", None),
        ("patch", "/api/v1/costs/{cost}", {"title": "x"}),
        ("patch", "/api/v1/proceedings/{proc}", {"az_court": "x"}),
        ("delete", "/api/v1/proceedings/{proc}", None),
        ("patch", "/api/v1/action-items/{item}", {"status": "completed"}),
        ("get", "/api/v1/cases/{case}/shares", None),
    ],
)
@pytest.mark.integration
def test_viewer_share_cannot_mutate(auth_enabled, db_session, dash, method, path, body):
    """A VIEWER share reads the dashboard but gets 404 from every mutation."""
    from app.models.database import CaseShare
    from app.models.enums import CaseAccessLevel
    from app.services import auth_service

    viewer = auth_service.create_user(
        db_session,
        email="viewer@example.com",
        password="password123",  # pragma: allowlist secret
    )
    db_session.add(
        CaseShare(
            case_id="DASH-001", user_id=viewer.id, permission=CaseAccessLevel.VIEWER
        )
    )
    db_session.commit()
    item = db_session.query(ActionItem).filter_by(case_id="DASH-001").one()
    c = TestClient(app, follow_redirects=False)
    c.post(
        "/api/v1/auth/login",
        json={
            "email": "viewer@example.com",
            "password": "password123",  # pragma: allowlist secret
        },
    )
    assert c.get("/api/v1/cases/DASH-001").status_code == 200
    url = path.format(
        case="DASH-001",
        claim=dash["claim"].id,
        cost=dash["cost"].id,
        proc=dash["p2"].id,
        item=item.id,
    )
    if body is not None:
        body = {
            k: (v.format(case="DASH-001") if isinstance(v, str) else v)
            for k, v in body.items()
        }
    resp = (
        getattr(c, method)(url, json=body)
        if body is not None
        else getattr(c, method)(url)
    )
    assert resp.status_code == 404, (url, resp.status_code, resp.text)
