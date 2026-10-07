"""Derived "recent activity" rows for Home (issue #176)."""

from datetime import timedelta

import pytest

from app.core.timezone import now_utc
from app.models.database import (
    ActionItem,
    Case,
    CaseShare,
    Document,
    DocumentPipelineStage,
    LegalCost,
)
from app.models.enums import (
    ActionItemType,
    CaseAccessLevel,
    CaseStatus,
    CostCategory,
    CostStatus,
    Jurisdiction,
    PipelineStage,
    StageStatus,
)
from app.services import auth_service
from app.services.home_activity import recent_activity

pytestmark = pytest.mark.unit

PASSWORD = "password123"  # pragma: allowlist secret


def _case(db, case_id, owner_id, **kw):
    case = Case(
        id=case_id,
        title=f"Case {case_id}",
        status=CaseStatus.INTAKE,
        jurisdiction=Jurisdiction.DE,
        owner_id=owner_id,
        **kw,
    )
    db.add(case)
    db.commit()
    return case


@pytest.fixture
def users(db_session):
    a = auth_service.create_user(db_session, email="a@example.com", password=PASSWORD)
    b = auth_service.create_user(db_session, email="b@example.com", password=PASSWORD)
    db_session.commit()
    return a, b


def test_every_source_contributes_newest_first(db_session, users):
    a, b = users
    now = now_utc()
    _case(db_session, "ACT-1", a.id, closed_at=now - timedelta(hours=7))
    _case(db_session, "ACT-2", a.id, ai_brief_updated_at=now - timedelta(hours=6))
    doc = Document(
        title="Klageerwiderung.pdf",
        case_id="ACT-1",
        ingest_date=now - timedelta(hours=8),
    )
    db_session.add(doc)
    db_session.flush()
    db_session.add_all(
        [
            DocumentPipelineStage(
                document_id=doc.id,
                stage=PipelineStage.ENRICH,
                status=StageStatus.COMPLETED,
                completed_at=now - timedelta(hours=5),
            ),
            DocumentPipelineStage(
                document_id=doc.id,
                stage=PipelineStage.CLAIMS,
                status=StageStatus.FAILED,
                error="model timeout",
                completed_at=now - timedelta(hours=4),
            ),
            DocumentPipelineStage(
                document_id=doc.id,
                stage=PipelineStage.EXTRACT,
                status=StageStatus.COMPLETED,
                completed_at=now - timedelta(hours=9),
            ),
            ActionItem(
                case_id="ACT-1",
                title="Frist Stellungnahme",
                due_date=now + timedelta(days=10),
                action_type=ActionItemType.DEADLINE,
                ingest_date=now - timedelta(hours=3),
            ),
            ActionItem(
                case_id="ACT-1",
                title="Termin",
                due_date=now + timedelta(days=20),
                action_type=ActionItemType.COURT_DATE,
                ingest_date=now - timedelta(hours=2),
            ),
            LegalCost(
                case_id="ACT-2",
                category=CostCategory.GERICHTSKOSTEN,
                status=CostStatus.BEZAHLT,
                title="Vorschuss",
                amount_net=100.0,
                amount_gross=100.0,
                paid_at=now - timedelta(hours=1),
            ),
            CaseShare(
                case_id="ACT-2",
                user_id=b.id,
                permission=CaseAccessLevel.VIEWER,
                created_at=now - timedelta(minutes=30),
            ),
        ]
    )
    db_session.commit()

    rows = recent_activity(db_session, a, None, limit=20)
    assert [r["kind"] for r in rows] == [
        "case_shared",
        "cost_paid",
        "hearing_scheduled",
        "deadline_extracted",
        "pipeline_failed",
        "document_enriched",
        "brief_refreshed",
        "case_closed",
        "document_ingested",
    ]
    assert rows[0]["detail"] == "shared with b@example.com · viewer"
    assert rows[1]["link"] == "/costs"
    assert rows[2]["link"] == "/cases/ACT-1?view=review"
    assert rows[4]["detail"] == "claims stage · model timeout"
    assert rows[5]["detail"] == "ACT-1 · Case ACT-1"
    assert rows[8]["link"] == f"/document/{doc.id}"
    # Only the enrich stage counts as "enriched"; extract completion is noise.
    assert all(r["title"] != "extract" for r in rows)


def test_scoped_to_visible_cases_and_capped(db_session, users):
    a, b = users
    now = now_utc()
    _case(db_session, "ACT-A", a.id)
    _case(db_session, "ACT-B", b.id)
    for i in range(12):
        db_session.add(
            Document(
                title=f"A doc {i}",
                case_id="ACT-A",
                ingest_date=now - timedelta(minutes=i),
            )
        )
    db_session.add(Document(title="B doc", case_id="ACT-B", ingest_date=now))
    db_session.commit()

    rows = recent_activity(db_session, a, ["ACT-A"], limit=8)
    assert len(rows) == 8
    assert all(r["case_id"] == "ACT-A" for r in rows)
    assert rows[0]["title"] == "A doc 0"
    assert recent_activity(db_session, a, [], limit=8) == []
    # Shares on cases the caller does not own are not their activity.
    db_session.add(
        CaseShare(case_id="ACT-B", user_id=a.id, permission=CaseAccessLevel.VIEWER)
    )
    db_session.commit()
    assert not any(
        r["kind"] == "case_shared" for r in recent_activity(db_session, a, None)
    )
