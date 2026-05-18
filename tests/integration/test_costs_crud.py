import pytest

from app.models.database import Document, LegalCost


@pytest.mark.integration
def test_costs_crud_flow(app_client, sample_case):
    # 1. Create a cost
    resp = app_client.post(
        "/costs",
        data={
            "case_id": sample_case.id,
            "category": "gerichtskosten",
            "title": "New Court Fee",
            "amount_net": "100.0",
            "vat_rate": "0.0",
            "status": "offen",
            "issued_at": "2026-04-01",
        },
    )
    assert resp.status_code == 200
    assert "New Court Fee" in resp.text

    # 2. Update a field
    # Need to find the ID. Since cleanup happens per test, it's likely ID 1.
    cost_id = 1
    resp = app_client.post(
        f"/costs/{cost_id}/update-field",
        data={"field": "title", "value": "Updated Fee Title"},
    )
    assert resp.status_code == 200
    assert "Updated Fee Title" in resp.text

    # 3. Pay the cost
    resp = app_client.post(f"/costs/{cost_id}/pay")
    assert resp.status_code == 200
    assert "PAID" in resp.text.upper()

    # 4. Reimburse
    resp = app_client.post(f"/costs/{cost_id}/reimburse", data={"amount": "100.0"})
    assert resp.status_code == 200
    assert "REIMBURSED" in resp.text.upper()


@pytest.mark.integration
def test_create_cost_persists_vat_rate(app_client, db_session, sample_case):
    """POST /costs with vat_rate stores the rate on the LegalCost row."""
    resp = app_client.post(
        "/costs",
        data={
            "case_id": sample_case.id,
            "category": "anwaltskosten",
            "title": "VAT Test",
            "amount_net": "100.0",
            "vat_rate": "0.19",
            "status": "offen",
        },
    )
    assert resp.status_code == 200

    row = (
        db_session.query(LegalCost)
        .filter(LegalCost.case_id == sample_case.id, LegalCost.title == "VAT Test")
        .first()
    )
    assert row is not None
    assert row.vat_rate == pytest.approx(0.19)
    assert row.amount_gross == pytest.approx(119.0)


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

    resp = app_client.post(f"/document/{doc.id}/cost-from-delta")
    assert resp.status_code == 200
    assert "promoted" in resp.text

    cost = (
        db_session.query(LegalCost)
        .filter(LegalCost.source_document_id == doc.id)
        .first()
    )
    assert cost is not None
    assert cost.amount_net == 450.0
    assert cost.amount_gross == 450.0  # no VAT override → 0%
