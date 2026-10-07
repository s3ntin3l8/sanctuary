import pytest

from app.models.database import Document, LegalCost


@pytest.mark.integration
def test_costs_crud_flow(app_client, sample_case):
    created = app_client.post(
        f"/api/v1/cases/{sample_case.id}/costs",
        json={
            "category": "gerichtskosten",
            "title": " New Court Fee ",
            "amount_net": 100.0,
            "vat_rate": 0.0,
            "issued_at": "2026-04-01T00:00:00Z",
        },
    )
    assert created.status_code == 201, created.text
    cost = created.json()
    assert cost["title"] == "New Court Fee"
    assert cost["amount_gross"] == 100.0
    cost_id = cost["id"]

    edited = app_client.patch(
        f"/api/v1/costs/{cost_id}", json={"title": "Updated Fee Title", "notes": "n"}
    )
    assert edited.status_code == 200
    assert edited.json()["title"] == "Updated Fee Title"
    assert edited.json()["notes"] == "n"

    paid = app_client.post(f"/api/v1/costs/{cost_id}/pay")
    assert paid.json()["status"] == "bezahlt"
    assert paid.json()["paid_at"] is not None

    reimbursed = app_client.post(
        f"/api/v1/costs/{cost_id}/reimburse", json={"amount": 100.0}
    )
    assert reimbursed.json()["status"] == "erstattet"

    overview = app_client.get("/api/v1/costs")
    assert overview.status_code == 200, overview.text
    group = next(g for g in overview.json()["cases"] if g["id"] == sample_case.id)
    assert [c["id"] for c in group["costs"]] == [cost_id]
    assert overview.json()["summary"]["booked"] == 100.0


@pytest.mark.integration
def test_patch_cost_clears_nullable_fields_and_keeps_explicit_status(
    app_client, sample_case
):
    created = app_client.post(
        f"/api/v1/cases/{sample_case.id}/costs",
        json={
            "category": "anwaltskosten",
            "title": "Fee",
            "amount_net": 100.0,
            "vat_rate": 0.19,
            "streitwert": 5000.0,
            "notes": "keep me",
            "due_at": "2026-05-01T00:00:00Z",
        },
    )
    cost_id = created.json()["id"]

    # An explicit null clears the field; fields left out stay untouched.
    cleared = app_client.patch(
        f"/api/v1/costs/{cost_id}", json={"streitwert": None, "due_at": None}
    )
    assert cleared.status_code == 200, cleared.text
    assert cleared.json()["streitwert"] is None
    assert cleared.json()["due_at"] is None
    assert cleared.json()["notes"] == "keep me"

    # A partial payment would derive "teilweise"; the explicit status wins.
    disputed = app_client.patch(
        f"/api/v1/costs/{cost_id}", json={"amount_paid": 10.0, "status": "strittig"}
    )
    assert disputed.status_code == 200, disputed.text
    assert disputed.json()["amount_paid"] == 10.0
    assert disputed.json()["status"] == "strittig"

    # Without an explicit status the amounts drive it.
    partial = app_client.patch(f"/api/v1/costs/{cost_id}", json={"amount_paid": 20.0})
    assert partial.json()["status"] == "teilweise"

    # Gross is rounded to cents when net or VAT changes.
    vat = app_client.patch(f"/api/v1/costs/{cost_id}", json={"vat_rate": 0.07})
    assert vat.json()["amount_gross"] == 107.0

    empty = app_client.patch(f"/api/v1/costs/{cost_id}", json={})
    assert empty.status_code == 422
    assert empty.json()["code"] == "empty_update"


@pytest.mark.integration
def test_create_cost_derives_gross_and_validates_proceeding(
    app_client, db_session, sample_case
):
    resp = app_client.post(
        f"/api/v1/cases/{sample_case.id}/costs",
        json={
            "category": "anwaltskosten",
            "title": "VAT Test",
            "amount_net": 100.0,
            "vat_rate": 0.19,
        },
    )
    assert resp.status_code == 201, resp.text
    row = (
        db_session.query(LegalCost)
        .filter(LegalCost.case_id == sample_case.id, LegalCost.title == "VAT Test")
        .first()
    )
    assert row is not None
    assert row.vat_rate == pytest.approx(0.19)
    assert row.amount_gross == pytest.approx(119.0)

    bad = app_client.post(
        f"/api/v1/cases/{sample_case.id}/costs",
        json={
            "category": "anwaltskosten",
            "title": "Wrong proceeding",
            "amount_net": 1.0,
            "proceeding_id": 999999,
        },
    )
    assert bad.status_code == 422
    assert bad.json()["code"] == "bad_proceeding"


@pytest.mark.integration
def test_costs_overview_lists_overdue_and_due_soon(app_client, db_session, sample_case):
    from datetime import UTC, datetime, timedelta

    from app.models.enums import CostCategory, CostStatus

    now = datetime.now(UTC)
    db_session.add_all(
        [
            LegalCost(
                case_id=sample_case.id,
                category=CostCategory.GERICHTSKOSTEN,
                status=CostStatus.OFFEN,
                title="Late",
                amount_net=10,
                amount_gross=10,
                due_at=now - timedelta(days=3),
            ),
            LegalCost(
                case_id=sample_case.id,
                category=CostCategory.GERICHTSKOSTEN,
                status=CostStatus.OFFEN,
                title="Soon",
                amount_net=20,
                amount_gross=20,
                amount_paid=5,
                due_at=now + timedelta(days=2),
            ),
            LegalCost(
                case_id=sample_case.id,
                category=CostCategory.GERICHTSKOSTEN,
                status=CostStatus.BEZAHLT,
                title="Settled",
                amount_net=30,
                amount_gross=30,
                due_at=now - timedelta(days=30),
            ),
        ]
    )
    db_session.commit()
    body = app_client.get("/api/v1/costs").json()
    assert [a["cost"]["title"] for a in body["overdue"]] == ["Late"]
    assert [a["cost"]["title"] for a in body["due_soon"]] == ["Soon"]
    assert body["due_soon"][0]["open_amount"] == 15.0
    assert body["cases"][0]["can_edit"] is True


@pytest.mark.integration
def test_promote_cost_delta(app_client, db_session, sample_case):
    """Promote a CostSignal (e.g. streitwert) into a LegalCost ledger row."""
    from datetime import datetime

    from app.models.database import CostSignal
    from app.models.enums import CostSignalType

    doc = Document(
        case_id=sample_case.id,
        title="Streitwertbeschluss",
        ingest_date=datetime.now(),
    )
    db_session.add(doc)
    db_session.flush()
    db_session.add(
        CostSignal(
            case_id=sample_case.id,
            source_document_id=doc.id,
            signal_type=CostSignalType.STREITWERT,
            amount=450.0,
            description="Streitwert für Klage",
        )
    )
    db_session.commit()

    resp = app_client.post(f"/api/v1/documents/{doc.id}/cost-signals/promote", json={})
    assert resp.status_code == 200
    assert resp.json()["amount_gross"] == 450.0

    cost = (
        db_session.query(LegalCost)
        .filter(LegalCost.source_document_id == doc.id)
        .first()
    )
    assert cost is not None
    assert cost.amount_net == 450.0
    assert cost.amount_gross == 450.0  # no VAT override → 0%
