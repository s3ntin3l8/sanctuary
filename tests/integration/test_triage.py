import pytest

from app.models.database import (
    Case,
    CaseStatus,
    Document,
)


# Lazy module-level TestClient — defers TestClient construction until the
# first attribute access (i.e., inside a test body, after `setup_test_db`
# autouse has run). Avoids the timing fragility of module-level
# `client = TestClient(app)` flagged in the code review.
class _LazyTestClient:
    _real = None

    def __getattr__(self, attr):
        if _LazyTestClient._real is None:
            from fastapi.testclient import TestClient

            from app.main import app

            _LazyTestClient._real = TestClient(app)
        return getattr(_LazyTestClient._real, attr)


client = _LazyTestClient()


@pytest.mark.integration
def test_triage_page_renders(db_session):
    """Test triage page renders without errors."""
    response = client.get("/triage")
    assert response.status_code == 200


@pytest.mark.integration
def test_triage_with_pending_docs(db_session):
    """Test triage page shows pending documents."""
    doc = Document(
        title="Test Document for Review",
        case_id=None,
        needs_review=True,
    )
    db_session.add(doc)
    db_session.commit()

    response = client.get("/triage")
    assert response.status_code == 200


@pytest.mark.integration
def test_triage_with_case_mapped(db_session):
    """Test triage page shows case-mapped documents."""
    case = Case(id="TRIAGE-TEST-001", title="Test Case", status=CaseStatus.INTAKE)
    db_session.add(case)
    db_session.commit()

    doc = Document(
        title="Document with Case",
        case_id="TRIAGE-TEST-001",
        needs_review=True,
    )
    db_session.add(doc)
    db_session.commit()

    response = client.get("/triage")
    assert response.status_code == 200


@pytest.mark.integration
def test_triage_doc_pane_renders_embedded_hud(db_session):
    """HX request to /document/:id returns the embedded HUD (no ?context=triage needed)."""
    doc = Document(
        title="Embedded HUD Test Doc",
        content="Some content",
        case_id=None,
        needs_review=True,
    )
    db_session.add(doc)
    db_session.commit()

    response = client.get(
        f"/document/{doc.id}",
        headers={"HX-Request": "true"},
    )
    assert response.status_code == 200
    assert 'data-hud-context="embedded"' in response.text
    assert f'data-doc-id="{doc.id}"' in response.text


@pytest.mark.integration
def test_triage_reaction_routes_removed(db_session):
    """The old triage reaction endpoints must no longer exist."""
    doc = Document(title="Reaction Test Doc", needs_review=True)
    db_session.add(doc)
    db_session.commit()

    r = client.post(
        f"/triage/document/{doc.id}/reaction",
        data={"reaction": "true"},
    )
    assert r.status_code == 404 or r.status_code == 405

    r = client.request(
        "DELETE",
        f"/triage/document/{doc.id}/reaction/true",
    )
    assert r.status_code == 404 or r.status_code == 405


@pytest.mark.integration
def test_unified_reaction_route_returns_hud_fragment(db_session):
    """POST /document/:id/reaction returns the _reactions.html fragment."""
    doc = Document(title="Reaction Fragment Test", needs_review=True)
    db_session.add(doc)
    db_session.commit()

    response = client.post(
        f"/document/{doc.id}/reaction",
        data={"reaction": "true"},
    )
    assert response.status_code == 200
    assert "hud-reaction-bar" in response.text
    assert "data-triage-reaction-bar" in response.text
    assert 'data-reaction-key="true"' in response.text


@pytest.mark.integration
def test_unified_reaction_emits_note_saved_toast(db_session):
    """POST /document/:id/reaction with notes emits a showToast trigger."""
    doc = Document(title="Note Saved Test", needs_review=True)
    db_session.add(doc)
    db_session.commit()

    response = client.post(
        f"/document/{doc.id}/reaction",
        data={"reaction": "true", "notes": "Important evidence"},
    )
    assert response.status_code == 200
    hx_trigger = response.headers.get("hx-trigger", "")
    assert "showToast" in hx_trigger
    assert "Note saved" in hx_trigger


@pytest.mark.integration
def test_triage_case_selector(db_session):
    """Test triage page includes case selector."""
    response = client.get("/triage")
    assert response.status_code == 200


# ── Case-creation overhaul: HX-Trigger payloads + draft surfacing ────────


import json as _json


def _make_doc_on_draft(db, case_id="DRAFT-AAA-1", title="Draft Test Case"):
    case = Case(id=case_id, title=title, status=CaseStatus.INTAKE, is_draft=True)
    db.add(case)
    db.flush()
    doc = Document(title="On Draft Doc", case_id=case_id, needs_review=True)
    db.add(doc)
    db.commit()
    return case, doc


@pytest.mark.integration
def test_hud_dropdown_includes_in_context_draft(db_session):
    """When a doc is on a draft case, the case selector must surface that
    draft prominently (it used to be the form's Case <select>; case + case-id
    moved out of the form into the dedicated case selector partial above)."""
    case, doc = _make_doc_on_draft(db_session, case_id="DRAFT-VIS-1")
    response = client.get(
        f"/document/{doc.id}?context=triage",
        headers={"HX-Request": "true"},
    )
    assert response.status_code == 200
    # Draft case id surfaces in the case section.
    assert "DRAFT-VIS-1" in response.text
    # And it's flagged as a draft via the auto-created caption / draft pill.
    assert "AI auto-created" in response.text or "draft" in response.text.lower()


@pytest.mark.integration
def test_hud_case_section_renders_for_draft(db_session):
    """The new Case HUD section renders ratify/reject buttons for a draft."""
    case, doc = _make_doc_on_draft(db_session, case_id="DRAFT-SEC-1")
    response = client.get(
        f"/document/{doc.id}?context=triage",
        headers={"HX-Request": "true"},
    )
    assert response.status_code == 200
    # Confirms-draft endpoint button must be present
    assert "/cases/DRAFT-SEC-1/confirm-draft?context=triage" in response.text
    assert "/cases/DRAFT-SEC-1/reject-draft?context=triage" in response.text


@pytest.mark.integration
def test_per_doc_confirm_emits_case_confirmed_trigger(db_session):
    """POST /triage/document/:id/confirm emits case:confirmed when doc lands
    in a real case."""
    case = Case(id="REAL-CC-1", title="Real Case", status=CaseStatus.INTAKE)
    db_session.add(case)
    doc = Document(title="To Confirm", case_id=None, needs_review=True)
    db_session.add(doc)
    db_session.commit()

    response = client.post(
        f"/triage/document/{doc.id}/confirm",
        data={
            "title": "To Confirm",
            "case_id": "REAL-CC-1",
            "originator_type": "court",
            "sender": "Test Court",
            "issued_date": "2026-01-01",
            "received_date": "2026-01-02",
        },
    )
    assert response.status_code == 200
    trigger_header = response.headers.get("HX-Trigger")
    assert trigger_header is not None
    payload = _json.loads(trigger_header)
    assert "case:confirmed" in payload
    assert payload["case:confirmed"]["case_id"] == "REAL-CC-1"
    assert payload["case:confirmed"]["case_title"] == "Real Case"
    assert payload["case:confirmed"]["action"] == "assigned"


@pytest.mark.integration
def test_confirm_draft_emits_ratified_trigger(db_session):
    """POST /cases/:id/confirm-draft emits case:confirmed with action=ratified."""
    case, doc = _make_doc_on_draft(db_session, case_id="DRAFT-RAT-1")

    response = client.post("/cases/DRAFT-RAT-1/confirm-draft")
    assert response.status_code == 200
    trigger_header = response.headers.get("HX-Trigger")
    assert trigger_header is not None
    payload = _json.loads(trigger_header)
    assert "case:confirmed" in payload
    assert payload["case:confirmed"]["case_id"] == "DRAFT-RAT-1"
    assert payload["case:confirmed"]["action"] == "ratified"

    db_session.refresh(case)
    assert case.is_draft is False


@pytest.mark.integration
def test_reject_draft_emits_rejected_trigger(db_session):
    """POST /cases/:id/reject-draft emits case:rejected."""
    case, doc = _make_doc_on_draft(db_session, case_id="DRAFT-REJ-1")

    response = client.post("/cases/DRAFT-REJ-1/reject-draft")
    assert response.status_code == 200
    trigger_header = response.headers.get("HX-Trigger")
    assert trigger_header is not None
    payload = _json.loads(trigger_header)
    assert "case:rejected" in payload
    assert payload["case:rejected"]["case_id"] == "DRAFT-REJ-1"


@pytest.mark.integration
def test_orphaned_draft_auto_cleanup_on_reassign(db_session):
    """When the last doc on a draft case is moved away, the draft Case row
    is auto-deleted. Drafts that still have docs are left alone."""
    real_case = Case(id="REAL-CLEAN-1", title="Real Case", status=CaseStatus.INTAKE)
    draft = Case(
        id="DRAFT-ORPH-1",
        title="Draft Cleanup",
        status=CaseStatus.INTAKE,
        is_draft=True,
    )
    keeper = Case(
        id="DRAFT-KEEP-1",
        title="Draft Keeper",
        status=CaseStatus.INTAKE,
        is_draft=True,
    )
    db_session.add_all([real_case, draft, keeper])
    db_session.flush()
    moved_doc = Document(
        title="Mover",
        case_id="DRAFT-ORPH-1",
        sender="Court",
        needs_review=True,
    )
    keeper_doc = Document(
        title="Stays on Keeper",
        case_id="DRAFT-KEEP-1",
        needs_review=True,
    )
    db_session.add_all([moved_doc, keeper_doc])
    db_session.commit()

    # Confirm: move the doc to the real case (last on draft → orphan).
    response = client.post(
        f"/triage/document/{moved_doc.id}/confirm",
        data={
            "title": "Mover",
            "case_id": "REAL-CLEAN-1",
            "originator_type": "court",
            "sender": "Court",
            "issued_date": "2026-01-01",
            "received_date": "2026-01-02",
        },
    )
    assert response.status_code == 200
    db_session.expire_all()
    assert db_session.query(Case).filter(Case.id == "DRAFT-ORPH-1").first() is None, (
        "Orphaned draft should have been auto-deleted"
    )
    # Keeper draft survives — its doc is still attached.
    assert db_session.query(Case).filter(Case.id == "DRAFT-KEEP-1").first() is not None


@pytest.mark.integration
def test_confirm_persists_significance_tier_and_document_type(db_session):
    """Per-doc confirm now accepts significance_tier + document_type from the
    review form so AI mistakes are correctable in place."""
    case = Case(id="REAL-EDIT-1", title="Edit Test", status=CaseStatus.INTAKE)
    db_session.add(case)
    doc = Document(title="Editable", case_id=None, needs_review=True)
    db_session.add(doc)
    db_session.commit()

    response = client.post(
        f"/triage/document/{doc.id}/confirm",
        data={
            "title": "Editable",
            "case_id": "REAL-EDIT-1",
            "originator_type": "court",
            "sender": "Court",
            "issued_date": "2026-01-01",
            "received_date": "2026-01-02",
            "significance_tier": "critical",
            "document_type": "ruling",
        },
    )
    assert response.status_code == 200
    db_session.expire(doc)
    db_session.refresh(doc)
    assert doc.significance_tier.value == "critical"
    assert doc.document_type.value == "ruling"
