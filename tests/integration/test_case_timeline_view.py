import pytest
from datetime import datetime, timedelta
from app.models.database import Document
from app.services.case_dashboard_service import CaseDashboardService

@pytest.mark.integration
def test_timeline_fallback_logic(db_session, sample_case):
    # Case with docs but 0 edges -> should fallback to timeline
    doc = Document(
        case_id=sample_case.id,
        title="Alone Doc",
        ingest_date=datetime.now()
    )
    db_session.add(doc)
    db_session.commit()

    service = CaseDashboardService(db_session)
    context = service.build_context(
        case_id=sample_case.id,
        active_proceeding_id=None,
        active_view="graph" # user wants graph, but we have 0 edges
    )

    assert context["initial"]["view"] == "timeline"

@pytest.mark.integration
def test_timeline_rendering_order(app_client, db_session, sample_case):
    # Create two docs with different issued_dates
    d1 = Document(
        case_id=sample_case.id,
        title="Older",
        issued_date=datetime(2026, 1, 1),
        ingest_date=datetime.now()
    )
    d2 = Document(
        case_id=sample_case.id,
        title="Newer",
        issued_date=datetime(2026, 1, 10),
        ingest_date=datetime.now()
    )
    db_session.add_all([d1, d2])
    db_session.commit()

    # Toggling view=timeline should show them in descending order
    resp = app_client.get(f"/cases/{sample_case.id}?view=timeline")
    assert resp.status_code == 200
    
    # Simple check for order in text (descending)
    text = resp.text
    idx_newer = text.find("Newer")
    idx_older = text.find("Older")
    assert idx_newer != -1
    assert idx_older != -1
    assert idx_newer < idx_older
