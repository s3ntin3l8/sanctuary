"""Integration tests for Phase 8 — case dashboard, graph partial, HUD slide-in,
and the user-settings persistence endpoints.

All tests drive the FastAPI TestClient and rely on the `db_session` fixture
from `tests/conftest.py` for seeding.
"""

from datetime import datetime

import pytest
from fastapi.testclient import TestClient

from app.main import app
from app.models.database import (
    Case,
    Document,
    Proceeding,
    UserSettings,
)
from app.models.enums import (
    CaseStatus,
    DocumentRole,
    Jurisdiction,
    OriginatorType,
    ProceedingCourtLevel,
    ProceedingStatus,
    SignificanceTier,
)
from app.services.user_settings_service import (
    get_active_proceeding,
    get_dashboard_view,
)

client = TestClient(app)


# ---------------------------------------------------------------------------
# Fixtures — build a case + proceeding + one document so the dashboard renders
# its full graph-view path (SVG included).
# ---------------------------------------------------------------------------


@pytest.fixture
def graph_case(db_session) -> Case:
    case = Case(
        id="GRAPH-001",
        title="Graph Dashboard Test Case",
        status=CaseStatus.INTAKE,
        jurisdiction=Jurisdiction.DE,
    )
    db_session.add(case)
    db_session.flush()
    return case


@pytest.fixture
def graph_proceeding(db_session, graph_case) -> Proceeding:
    proceeding = Proceeding(
        case_id=graph_case.id,
        court_name="Amtsgericht Hamburg",
        court_level=ProceedingCourtLevel.AG,
        az_court="003 F 426/25",
        status=ProceedingStatus.ACTIVE,
        started_at=datetime(2025, 1, 1),
        created_at=datetime(2025, 1, 1),
    )
    db_session.add(proceeding)
    db_session.flush()
    db_session.refresh(proceeding)
    return proceeding


@pytest.fixture
def graph_document(db_session, graph_case, graph_proceeding) -> Document:
    doc = Document(
        title="Klageschrift",
        content="Die Klage wird erhoben...",
        case_id=graph_case.id,
        proceeding_id=graph_proceeding.id,
        originator_type=OriginatorType.OPPOSING,
        role=DocumentRole.STANDALONE,
        significance_tier=SignificanceTier.CRITICAL,
        received_date=datetime(2025, 1, 15),
    )
    db_session.add(doc)
    db_session.flush()
    db_session.refresh(doc)
    return doc


# ===========================================================================
# Dashboard routes
# ===========================================================================


class TestCaseDashboardGraph:
    @pytest.mark.integration
    def test_case_dashboard_loads(
        self, db_session, graph_case, graph_proceeding, graph_document
    ):
        db_session.commit()
        response = client.get(f"/cases/{graph_case.id}")
        assert response.status_code == 200
        assert "Graph Dashboard Test Case" in response.text
        # SVG is rendered only when graph payload is built (i.e. an active
        # proceeding exists). That's the primary Phase-8 UI contract.
        assert "<svg" in response.text

    @pytest.mark.integration
    def test_view_graph_param_accepted(
        self, db_session, graph_case, graph_proceeding, graph_document
    ):
        db_session.commit()
        response = client.get(f"/cases/{graph_case.id}?view=graph")
        assert response.status_code == 200

    @pytest.mark.integration
    def test_view_truth_param_accepted(
        self, db_session, graph_case, graph_proceeding, graph_document
    ):
        db_session.commit()
        response = client.get(f"/cases/{graph_case.id}?view=truth")
        assert response.status_code == 200

    @pytest.mark.integration
    def test_proceeding_param_persists(
        self, db_session, graph_case, graph_proceeding, graph_document
    ):
        db_session.commit()
        # First visit: explicit proceeding query param should be persisted.
        response = client.get(
            f"/cases/{graph_case.id}?proceeding={graph_proceeding.id}"
        )
        assert response.status_code == 200

        # Expire the session so we read the latest UserSettings row written by
        # the request handler's own session.
        db_session.expire_all()
        assert get_active_proceeding(graph_case.id, db_session) == graph_proceeding.id

    @pytest.mark.integration
    def test_graph_partial_route(
        self, db_session, graph_case, graph_proceeding, graph_document
    ):
        db_session.commit()
        response = client.get(
            f"/cases/{graph_case.id}/graph?proceeding={graph_proceeding.id}"
        )
        assert response.status_code == 200
        assert "<svg" in response.text

    @pytest.mark.integration
    def test_graph_partial_route_unknown_case_404(self, db_session):
        response = client.get("/cases/NOPE-999-Z/graph")
        assert response.status_code == 404

    @pytest.mark.integration
    def test_document_hud_route(
        self, db_session, graph_case, graph_proceeding, graph_document
    ):
        db_session.commit()
        response = client.get(
            f"/cases/{graph_case.id}/document/{graph_document.id}/hud"
        )
        assert response.status_code == 200
        assert "DOCUMENT HUD" in response.text

    @pytest.mark.integration
    def test_document_hud_wrong_case_returns_404(
        self, db_session, graph_case, graph_proceeding, graph_document
    ):
        db_session.commit()
        response = client.get(f"/cases/WRONG-CASE/document/{graph_document.id}/hud")
        assert response.status_code == 404

    @pytest.mark.integration
    def test_document_hud_unknown_doc_returns_404(
        self, db_session, graph_case, graph_proceeding
    ):
        db_session.commit()
        response = client.get(f"/cases/{graph_case.id}/document/999999/hud")
        assert response.status_code == 404


# ===========================================================================
# UserSettings — persistence endpoints
# ===========================================================================


class TestUserSettingsDashboard:
    @pytest.mark.integration
    def test_post_dashboard_view(self, db_session):
        # Pre-seed an empty settings row so the service can update it.
        db_session.add(UserSettings(user_id="single_user", settings_json={}))
        db_session.commit()

        response = client.post(
            "/api/user-settings/dashboard-view", json={"view": "truth"}
        )
        assert response.status_code == 204

        db_session.expire_all()
        assert get_dashboard_view(db_session) == "truth"

    @pytest.mark.integration
    def test_post_dashboard_view_creates_settings_row_when_missing(self, db_session):
        # No pre-seeded UserSettings row — the endpoint must create one.
        response = client.post(
            "/api/user-settings/dashboard-view", json={"view": "graph"}
        )
        assert response.status_code == 204

        db_session.expire_all()
        assert get_dashboard_view(db_session) == "graph"

    @pytest.mark.integration
    def test_post_active_proceeding(self, db_session, graph_case, graph_proceeding):
        db_session.commit()
        response = client.post(
            f"/api/user-settings/active-proceeding/{graph_case.id}",
            json={"proceeding_id": graph_proceeding.id},
        )
        assert response.status_code == 204

        db_session.expire_all()
        assert get_active_proceeding(graph_case.id, db_session) == graph_proceeding.id
