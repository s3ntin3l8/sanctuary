"""Merging a duplicate proceeding or case folds its rows into the survivor."""

import shutil
from datetime import UTC, datetime

import pytest
from fastapi.testclient import TestClient

from app.main import app
from app.models.database import (
    ActionItem,
    Case,
    CaseShare,
    Document,
    Entity,
    IngestBatch,
    LegalCost,
    Proceeding,
    User,
)
from app.models.enums import (
    ActionItemType,
    CaseAccessLevel,
    CaseStatus,
    CostCategory,
    CostStatus,
    IngestBatchSourceType,
    IngestBatchStatus,
    ProceedingCourtLevel,
    ProceedingStatus,
)
from app.services.merge_service import MergeError, merge_cases, merge_proceedings

pytestmark = pytest.mark.integration

client = TestClient(app)
DUE = datetime(2026, 12, 1)


def _admin(db):
    return db.query(User).filter_by(email="admin@localhost").one()


def _case(db, case_id, **kw):
    case = Case(
        id=case_id,
        title=case_id,
        status=CaseStatus.INTAKE,
        owner_id=_admin(db).id,
        **kw,
    )
    db.add(case)
    db.flush()
    return case


def _proc(db, case_id, az, level=ProceedingCourtLevel.AG, **kw):
    proc = Proceeding(
        case_id=case_id,
        court_name="Amtsgericht Ingolstadt",
        court_level=level,
        az_court=az,
        status=kw.pop("status", ProceedingStatus.ACTIVE),
        is_draft=kw.pop("is_draft", False),
        **kw,
    )
    db.add(proc)
    db.flush()
    return proc


def _rows(db, case_id, proc_id):
    """One of every table that points at a proceeding."""
    owner = _admin(db).id
    batch = IngestBatch(
        owner_id=owner,
        source_type=IngestBatchSourceType.EMAIL,
        status=IngestBatchStatus.OPEN,
        received_at=datetime(2026, 10, 9, tzinfo=UTC),
        case_id=case_id,
        proceeding_id=proc_id,
    )
    db.add(batch)
    doc = Document(title="d", owner_id=owner, case_id=case_id, proceeding_id=proc_id)
    db.add(doc)
    db.add(
        ActionItem(
            case_id=case_id,
            proceeding_id=proc_id,
            title="t",
            due_date=DUE,
            action_type=ActionItemType.DEADLINE,
        )
    )
    db.flush()
    return batch, doc


def test_merge_proceedings_moves_every_reference_and_deletes_source(db_session):
    _case(db_session, "M-1")
    target = _proc(db_session, "M-1", "2 K 92/25", is_draft=True)
    source = _proc(
        db_session,
        "M-1",
        "2 K 92/25",
        level=ProceedingCourtLevel.LG,
        status=ProceedingStatus.ACTIVE,
        subject_matter="Teilung",
    )
    target.status = ProceedingStatus.CLOSED
    batch, doc = _rows(db_session, "M-1", source.id)
    cost = LegalCost(
        case_id="M-1",
        proceeding_id=source.id,
        category=CostCategory.GERICHTSKOSTEN,
        status=CostStatus.OFFEN,
        title="Gebühr",
        amount_net=10.0,
        amount_gross=10.0,
    )
    db_session.add(cost)
    db_session.commit()
    source_id, target_id = source.id, target.id

    merged = merge_proceedings(db_session, source_id, target_id)

    db_session.expire_all()
    assert db_session.get(Proceeding, source_id) is None
    assert db_session.get(Document, doc.id).proceeding_id == target_id
    assert db_session.get(IngestBatch, batch.id).proceeding_id == target_id
    item = db_session.query(ActionItem).filter_by(case_id="M-1").one()
    assert item.proceeding_id == target_id
    assert db_session.get(LegalCost, cost.id).proceeding_id == target_id
    assert merged.status == ProceedingStatus.ACTIVE  # source was active: reopened
    assert merged.subject_matter == "Teilung"
    assert merged.is_draft is False  # the confirmed source ratifies it


def test_merge_proceedings_rejects_same_and_cross_case(db_session):
    _case(db_session, "M-1")
    _case(db_session, "M-2")
    a = _proc(db_session, "M-1", "1 K 1/25")
    b = _proc(db_session, "M-2", "1 K 1/25")
    db_session.commit()
    with pytest.raises(MergeError) as same:
        merge_proceedings(db_session, a.id, a.id)
    assert same.value.code == "same_target"
    with pytest.raises(MergeError) as cross:
        merge_proceedings(db_session, a.id, b.id)
    assert cross.value.code == "cross_case"


def test_merge_cases_folds_twin_proceeding_actions_shares_and_parties(db_session):
    admin = _admin(db_session)
    other = User(email="ed@example.com", password_hash="x")
    db_session.add(other)
    db_session.flush()
    target = _case(
        db_session,
        "T-1",
        parties=[{"name": "Hansen", "role": "own"}],
        opposing_parties=["Liu"],
    )
    _case(
        db_session,
        "S-1",
        parties=[{"name": "hansen", "role": "own"}, {"name": "Weber", "role": "own"}],
        opposing_parties=["liu", "Li"],
        is_draft=True,
    )
    t_proc = _proc(db_session, "T-1", "2 K 92/25")
    s_twin = _proc(db_session, "S-1", "2 K 92/25", is_draft=True)
    s_only = _proc(db_session, "S-1", "22 T 1/26", level=ProceedingCourtLevel.LG)
    _rows(db_session, "T-1", t_proc.id)
    batch, doc = _rows(db_session, "S-1", s_twin.id)  # same due date/type as T-1's
    db_session.add(Entity(case_id="S-1", name="Liu", type="person"))
    db_session.add(
        CaseShare(case_id="S-1", user_id=other.id, permission=CaseAccessLevel.EDITOR)
    )
    db_session.add(
        CaseShare(case_id="T-1", user_id=other.id, permission=CaseAccessLevel.VIEWER)
    )
    db_session.commit()
    s_twin_id, s_only_id, doc_id = s_twin.id, s_only.id, doc.id
    assert admin.id == target.owner_id

    merged = merge_cases(db_session, "S-1", "T-1")

    db_session.expire_all()
    assert merged.id == "T-1"
    assert db_session.get(Case, "S-1") is None
    assert db_session.get(Proceeding, s_twin_id) is None  # absorbed by the twin
    assert db_session.get(Proceeding, s_only_id).case_id == "T-1"
    moved = db_session.get(Document, doc_id)
    assert (moved.case_id, moved.proceeding_id) == ("T-1", t_proc.id)
    assert db_session.get(IngestBatch, batch.id).case_id == "T-1"
    # the clashing deadline is dropped, the target's stays
    assert db_session.query(ActionItem).filter_by(case_id="T-1").count() == 1
    assert db_session.query(Entity).filter_by(case_id="T-1").count() == 1
    share = db_session.query(CaseShare).filter_by(case_id="T-1").one()
    assert share.permission == CaseAccessLevel.EDITOR  # higher level wins
    assert [p["name"] for p in merged.parties] == ["Hansen", "Weber"]
    assert merged.opposing_parties == ["Liu", "Li"]


def test_merge_cases_rejects_triage_and_self(db_session):
    _case(db_session, "T-1")
    db_session.commit()
    with pytest.raises(MergeError) as e:
        merge_cases(db_session, "T-1", "_TRIAGE")
    assert e.value.code == "triage_case"
    with pytest.raises(MergeError) as e:
        merge_cases(db_session, "T-1", "T-1")
    assert e.value.code == "same_target"


def test_merge_endpoints(db_session):
    _case(db_session, "API-1")
    _case(db_session, "API-2")
    keep = _proc(db_session, "API-1", "5 K 5/25")
    dupe = _proc(db_session, "API-1", "5 K 5/25", level=ProceedingCourtLevel.LG)
    db_session.commit()
    keep_id, dupe_id = keep.id, dupe.id
    from app.services import user_settings_service

    user_settings_service.set_active_proceeding(
        "API-1", dupe_id, db_session, _admin(db_session).id
    )
    db_session.commit()

    resp = client.post(
        f"/api/v1/proceedings/{dupe_id}/merge", json={"target_id": keep_id}
    )
    assert resp.status_code == 200, resp.text
    assert resp.json()["id"] == keep_id
    db_session.expire_all()
    assert (
        user_settings_service.get_active_proceeding(
            "API-1", db_session, _admin(db_session).id
        )
        == keep_id
    )
    bad = client.post(
        f"/api/v1/proceedings/{keep_id}/merge", json={"target_id": keep_id}
    )
    assert (bad.status_code, bad.json()["code"]) == (409, "same_target")

    resp = client.post("/api/v1/cases/API-2/merge", json={"target_id": "API-1"})
    assert resp.status_code == 200, resp.text
    assert resp.json() == {"case_id": "API-1"}
    db_session.expire_all()
    assert db_session.get(Case, "API-2") is None
    missing = client.post("/api/v1/cases/API-1/merge", json={"target_id": "NOPE"})
    assert missing.status_code == 404


def test_rename_case_moves_every_reference(db_session):
    from app.services import user_settings_service
    from app.services.merge_service import rename_case

    _case(db_session, "R-1")
    proc = _proc(db_session, "R-1", "1 F 1/26")
    batch, doc = _rows(db_session, "R-1", proc.id)
    user_id = _admin(db_session).id
    user_settings_service.mark_viewed("R-1", db_session, user_id)
    user_settings_service.set_active_proceeding("R-1", proc.id, db_session, user_id)
    db_session.commit()

    from app.config import DATA_DIR

    old_dir = DATA_DIR / "R-1" / "proc"
    old_dir.mkdir(parents=True, exist_ok=True)
    (old_dir / "a.pdf").write_text("x")
    doc.file_path = "R-1/proc/a.pdf"
    db_session.commit()

    renamed = rename_case(db_session, "R-1", "r-2/26", user_id)

    assert renamed.id == "R-2-26"
    db_session.expire_all()
    assert db_session.get(Case, "R-1") is None
    assert db_session.get(Document, doc.id).case_id == "R-2-26"
    assert db_session.get(Document, doc.id).file_path == "R-2-26/proc/a.pdf"
    assert (DATA_DIR / "R-2-26" / "proc" / "a.pdf").exists()
    assert not (DATA_DIR / "R-1").exists()
    shutil.rmtree(DATA_DIR / "R-2-26")
    assert db_session.get(IngestBatch, batch.id).case_id == "R-2-26"
    assert db_session.get(Proceeding, proc.id).case_id == "R-2-26"
    assert db_session.query(ActionItem).filter_by(case_id="R-2-26").count() == 1
    assert user_settings_service.get_last_viewed("R-1", db_session, user_id) is None
    assert user_settings_service.get_last_viewed("R-2-26", db_session, user_id)
    assert (
        user_settings_service.get_active_proceeding("R-2-26", db_session, user_id)
        == proc.id
    )


def test_rename_case_endpoint_guards(db_session):
    _case(db_session, "RE-1")
    _case(db_session, "RE-2")
    db_session.commit()

    taken = client.post("/api/v1/cases/RE-1/rename", json={"new_id": "RE-2"})
    assert (taken.status_code, taken.json()["code"]) == (409, "case_id_taken")
    same = client.post("/api/v1/cases/RE-1/rename", json={"new_id": "re-1"})
    assert (same.status_code, same.json()["code"]) == (409, "same_target")
    bad = client.post("/api/v1/cases/RE-1/rename", json={"new_id": "a b"})
    assert (bad.status_code, bad.json()["code"]) == (422, "invalid_case_id")
    ok = client.post("/api/v1/cases/RE-1/rename", json={"new_id": "RE-9"})
    assert ok.status_code == 200, ok.text
    assert ok.json()["id"] == "RE-9"
