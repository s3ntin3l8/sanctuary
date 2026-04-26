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
def test_promote_cost_delta(app_client, db_session, sample_case):
    # Setup doc with cost_delta
    doc = Document(
        case_id=sample_case.id,
        title="Cost Doc",
        cost_delta={"amount": 450.0, "direction": "incoming", "description": "Refund"},
        ingest_date=None,  # datetime handled by model? wait, conftest says ingest_date is not null
    )
    from datetime import datetime

    doc.ingest_date = datetime.now()
    db_session.add(doc)
    db_session.commit()

    resp = app_client.post(f"/document/{doc.id}/cost-from-delta")
    assert resp.status_code == 200
    assert "promoted" in resp.text

    # Verify LegalCost was created correctly
    cost = (
        db_session.query(LegalCost)
        .filter(LegalCost.source_document_id == doc.id)
        .first()
    )
    assert cost is not None
    assert cost.amount_net == 450.0
    assert cost.amount_gross == 450.0  # incoming -> 0 VAT
