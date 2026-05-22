"""Tests for app.services.intelligence.orchestrator — atomic dispatch claims.

claim_case_brief_for_dispatch must:
 - Return False when any doc in the case has CLAIMS not in a terminal state.
 - Return True (and set brief_queued_at) for the FIRST caller when readiness
   is satisfied.
 - Return False for subsequent callers until brief_queued_at is cleared
   (release_case_brief_claim).
"""

import pytest
from sqlalchemy import text

from app.models.database import Case, Document
from app.models.enums import CaseStatus, OriginatorType
from app.services.intelligence.orchestrator import (
    claim_case_brief_for_dispatch,
    release_case_brief_claim,
)


def _set_claims(db, doc_id: int, status: str) -> None:
    db.execute(
        text(
            """
            INSERT INTO document_pipeline_stages (document_id, stage, status)
            VALUES (:doc_id, 'claims', :status)
            ON CONFLICT(document_id, stage) DO UPDATE SET status=:status
            """
        ),
        {"doc_id": doc_id, "status": status},
    )
    db.commit()


@pytest.fixture
def case_with_three_docs(db_session):
    case = Case(id="ORCH-001", title="Orchestrator Test", status=CaseStatus.INTAKE)
    db_session.add(case)
    db_session.commit()

    docs = []
    for i in range(3):
        d = Document(
            title=f"Doc {i}",
            content="x",
            case_id="ORCH-001",
            originator_type=OriginatorType.COURT,
        )
        db_session.add(d)
        docs.append(d)
    db_session.commit()
    for d in docs:
        db_session.refresh(d)
    return case, docs


@pytest.mark.unit
def test_claim_blocks_when_a_doc_is_still_running(db_session, case_with_three_docs):
    case, docs = case_with_three_docs
    _set_claims(db_session, docs[0].id, "completed")
    _set_claims(db_session, docs[1].id, "completed")
    _set_claims(db_session, docs[2].id, "running")  # not terminal

    assert claim_case_brief_for_dispatch("ORCH-001", db_session) is False
    db_session.refresh(case)
    assert case.brief_queued_at is None


@pytest.mark.unit
def test_claim_blocks_when_a_doc_has_no_claims_row(db_session, case_with_three_docs):
    """A doc without ANY claims row counts as not-terminal (pipeline didn't reach it)."""
    case, docs = case_with_three_docs
    _set_claims(db_session, docs[0].id, "completed")
    _set_claims(db_session, docs[1].id, "completed")
    # docs[2] has no claims row at all

    assert claim_case_brief_for_dispatch("ORCH-001", db_session) is False
    db_session.refresh(case)
    assert case.brief_queued_at is None


@pytest.mark.unit
def test_claim_succeeds_when_all_docs_terminal(db_session, case_with_three_docs):
    case, docs = case_with_three_docs
    _set_claims(db_session, docs[0].id, "completed")
    _set_claims(db_session, docs[1].id, "skipped")
    _set_claims(db_session, docs[2].id, "failed")

    assert claim_case_brief_for_dispatch("ORCH-001", db_session) is True
    db_session.refresh(case)
    assert case.brief_queued_at is not None


@pytest.mark.unit
def test_claim_is_idempotent(db_session, case_with_three_docs):
    """Once claimed, subsequent callers get False until release."""
    case, docs = case_with_three_docs
    for d in docs:
        _set_claims(db_session, d.id, "completed")

    assert claim_case_brief_for_dispatch("ORCH-001", db_session) is True
    assert claim_case_brief_for_dispatch("ORCH-001", db_session) is False
    assert claim_case_brief_for_dispatch("ORCH-001", db_session) is False


@pytest.mark.unit
def test_claim_blocks_when_skip_reason_is_gate_block(db_session, case_with_three_docs):
    """A doc whose CLAIMS is skipped because an upstream gate is still blocking
    (enrich_not_completed, batch_analysis_not_completed, etc.) is NOT actually
    done — it'll re-run when the gate clears. The brief readiness predicate
    must wait, not fire prematurely with empty claims data.

    This reproduces the 'brief fired immediately after batch_analyzer' bug:
    extract_claims_task hit the new ENRICH gate, marked CLAIMS=SKIPPED
    reason=enrich_not_completed, called _trigger_case_brief — and the old
    predicate (which counted any SKIPPED as terminal) let the brief fly."""
    from sqlalchemy import text

    case, docs = case_with_three_docs
    _set_claims(db_session, docs[0].id, "completed")
    _set_claims(db_session, docs[1].id, "completed")
    # Doc 2 was skipped because ENRICH hadn't completed — gate-block, not policy.
    db_session.execute(
        text(
            "INSERT INTO document_pipeline_stages (document_id, stage, status, reason) "
            "VALUES (:doc_id, 'claims', 'skipped', 'enrich_not_completed') "
            "ON CONFLICT(document_id, stage) DO UPDATE SET "
            "status='skipped', reason='enrich_not_completed'"
        ),
        {"doc_id": docs[2].id},
    )
    db_session.commit()

    assert claim_case_brief_for_dispatch("ORCH-001", db_session) is False
    db_session.refresh(case)
    assert case.brief_queued_at is None


@pytest.mark.unit
def test_claim_succeeds_when_skip_reason_is_policy(db_session, case_with_three_docs):
    """A policy-driven SKIP (administrative tier, manual upload) IS terminal —
    those docs are intentionally excluded from claim extraction and the brief
    should proceed with whatever claims the eligible docs produced."""
    from sqlalchemy import text

    case, docs = case_with_three_docs
    _set_claims(db_session, docs[0].id, "completed")
    _set_claims(db_session, docs[1].id, "completed")
    # Doc 2 was skipped because its significance_tier is administrative — policy.
    db_session.execute(
        text(
            "INSERT INTO document_pipeline_stages (document_id, stage, status, reason) "
            "VALUES (:doc_id, 'claims', 'skipped', 'ineligible_tier:administrative') "
            "ON CONFLICT(document_id, stage) DO UPDATE SET "
            "status='skipped', reason='ineligible_tier:administrative'"
        ),
        {"doc_id": docs[2].id},
    )
    db_session.commit()

    assert claim_case_brief_for_dispatch("ORCH-001", db_session) is True


@pytest.mark.unit
def test_release_allows_re_claim(db_session, case_with_three_docs):
    """After release_case_brief_claim, the next call can claim again — this
    is the "next wave of pipeline activity" path."""
    case, docs = case_with_three_docs
    for d in docs:
        _set_claims(db_session, d.id, "completed")

    assert claim_case_brief_for_dispatch("ORCH-001", db_session) is True
    release_case_brief_claim("ORCH-001", db_session)
    db_session.refresh(case)
    assert case.brief_queued_at is None
    assert claim_case_brief_for_dispatch("ORCH-001", db_session) is True
