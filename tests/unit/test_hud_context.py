"""Unit tests for build_hud_context — covers bundle navigation and embedded context keys."""

import pytest

from app.models.database import Case, CaseStatus, Document
from app.services.hud_context import build_hud_context


@pytest.fixture
def case_and_batch(db_session):
    case = Case(id="HUD-CTX-001", title="HUD Context Test", status=CaseStatus.INTAKE)
    db_session.add(case)
    db_session.flush()
    return case


@pytest.mark.unit
def test_build_hud_context_returns_required_keys(db_session, case_and_batch):
    doc = Document(title="Test Doc", case_id=case_and_batch.id)
    db_session.add(doc)
    db_session.commit()

    ctx = build_hud_context(db_session, doc)

    required = {
        "doc",
        "mode",
        "context",
        "case_id",
        "summary_bullets",
        "key_passages",
        "reactions",
        "grounds",
        "claims_status",
        "actions",
        "relationships_out",
        "relationships_in",
        "prev_doc_id",
        "next_doc_id",
        "originator_color",
        "passage_claim_map",
        "pins",
        "passage_pin_counts",
        "first_child_id",
        "bundle_prev_id",
        "bundle_next_id",
    }
    assert required.issubset(ctx.keys())


@pytest.mark.unit
def test_build_hud_context_default_mode_and_context(db_session, case_and_batch):
    doc = Document(title="Test Doc", case_id=case_and_batch.id)
    db_session.add(doc)
    db_session.commit()

    ctx = build_hud_context(db_session, doc)
    assert ctx["mode"] == "read"
    assert ctx["context"] == "overlay"


@pytest.mark.unit
def test_bundle_nav_middle_doc(db_session):
    """bundle_prev_id and bundle_next_id are correct for the middle of a 3-doc bundle.

    Regression for the bug where Document.id != doc.id was filtered before indexing,
    making doc.id never present in sibling_ids.
    """
    docs = []
    for i in range(3):
        d = Document(title=f"Bundle Doc {i}", ingest_batch_id=42)
        db_session.add(d)
        docs.append(d)
    db_session.commit()

    middle = docs[1]
    ctx = build_hud_context(db_session, middle)

    # Siblings are ordered by id; middle doc should have prev and next.
    assert ctx["bundle_prev_id"] == docs[0].id
    assert ctx["bundle_next_id"] == docs[2].id


@pytest.mark.unit
def test_bundle_nav_first_doc(db_session):
    docs = []
    for i in range(3):
        d = Document(title=f"Bundle First {i}", ingest_batch_id=55)
        db_session.add(d)
        docs.append(d)
    db_session.commit()

    ctx = build_hud_context(db_session, docs[0])
    assert ctx["bundle_prev_id"] is None
    assert ctx["bundle_next_id"] == docs[1].id


@pytest.mark.unit
def test_bundle_nav_last_doc(db_session):
    docs = []
    for i in range(3):
        d = Document(title=f"Bundle Last {i}", ingest_batch_id=66)
        db_session.add(d)
        docs.append(d)
    db_session.commit()

    ctx = build_hud_context(db_session, docs[2])
    assert ctx["bundle_prev_id"] == docs[1].id
    assert ctx["bundle_next_id"] is None


@pytest.mark.unit
def test_bundle_nav_single_doc(db_session):
    doc = Document(title="Lone Bundle Doc", ingest_batch_id=77)
    db_session.add(doc)
    db_session.commit()

    ctx = build_hud_context(db_session, doc)
    assert ctx["bundle_prev_id"] is None
    assert ctx["bundle_next_id"] is None


@pytest.mark.unit
def test_bundle_nav_no_batch(db_session):
    doc = Document(title="No Batch Doc", ingest_batch_id=None)
    db_session.add(doc)
    db_session.commit()

    ctx = build_hud_context(db_session, doc)
    assert ctx["bundle_prev_id"] is None
    assert ctx["bundle_next_id"] is None


@pytest.mark.unit
def test_embedded_context_with_cases_adds_triage_keys(db_session):
    """When cases is provided, embedded context adds OriginatorType and is_draft_case."""
    doc = Document(title="Embedded Doc", case_id=None)
    db_session.add(doc)
    db_session.commit()

    ctx = build_hud_context(
        db_session, doc, mode="review", context="embedded", cases=[]
    )
    assert ctx["context"] == "embedded"
    assert ctx["mode"] == "review"
    assert "cases" in ctx
    assert "OriginatorType" in ctx
    assert "is_draft_case" in ctx
    assert ctx["is_draft_case"] is False


@pytest.mark.unit
def test_embedded_context_without_cases_no_triage_keys(db_session):
    """Without cases, embedded context still injects OriginatorType but not the cases list."""
    doc = Document(title="Embedded No Cases", case_id=None)
    db_session.add(doc)
    db_session.commit()

    ctx = build_hud_context(db_session, doc, context="embedded")
    assert "cases" not in ctx
    assert "OriginatorType" in ctx  # always injected now (Fix #2)
    assert "is_draft_case" not in ctx


@pytest.mark.unit
def test_is_draft_case_true_for_draft(db_session):
    case = Case(
        id="DRAFT-HUD-001", title="Draft Case", status=CaseStatus.INTAKE, is_draft=True
    )
    db_session.add(case)
    db_session.flush()
    doc = Document(title="Draft Doc", case_id=case.id)
    db_session.add(doc)
    db_session.commit()

    ctx = build_hud_context(db_session, doc, context="embedded", cases=[case])
    assert ctx["is_draft_case"] is True


@pytest.mark.unit
def test_hud_context_handles_doc_with_no_data(db_session):
    """A bare document (no case, no batch, no content) renders without errors."""
    doc = Document(title="Bare Doc")
    db_session.add(doc)
    db_session.commit()

    ctx = build_hud_context(db_session, doc)
    assert ctx["reactions"] == []
    assert ctx["grounds"] == []
    assert ctx["actions"] == []
    assert ctx["relationships_out"] == []
    assert ctx["relationships_in"] == []
    assert ctx["pins"] == []
    assert ctx["bundle_prev_id"] is None
    assert ctx["bundle_next_id"] is None
