"""Pin: CaseService.delete_and_revert handles the full cascade.

Refactor target: app/api/cases.py:528-622 (delete_case route) — 90+ LOC
of business logic embedded in the route. Move to CaseService so both
DELETE /cases/:id and POST /cases/:id/reject-draft call the same path.

Behaviours pinned here:
- Returns a dict with the affected document list and counts (so the route
  can build its OOB response).
- Documents are reverted to "_TRIAGE" with needs_review=True and
  proceeding_id=None (so they reappear in the Triage Inbox).
- IngestBatches that pointed at this case are reverted to case_id=None.
- Entity / ActionItem / LegalCost / Claim rows scoped to this case are
  deleted; ClaimEvidence cascades via the existing ORM relationship.
- The Case row itself is deleted last.
- Calling with a non-existent case returns None (caller raises 404).
- Calling with "_TRIAGE" raises a guard (Triage cannot be deleted).
"""

from datetime import datetime

import pytest

from app.models.database import (
    ActionItem,
    Case,
    Claim,
    ClaimEvidence,
    Document,
    Entity,
    IngestBatch,
    LegalCost,
)
from app.models.enums import (
    ActionItemType,
    CaseStatus,
    ClaimEvidenceRole,
    ClaimType,
    CostCategory,
    CostStatus,
    EntityType,
    IngestBatchSourceType,
)


@pytest.fixture
def case_with_full_dependencies(db_session):
    """Build a Case with one of every dependent row type."""
    case = Case(id="DEL-FULL-1", title="To delete", status=CaseStatus.INTAKE)
    db_session.add(case)
    batch = IngestBatch(source_type=IngestBatchSourceType.MANUAL, case_id=case.id)
    db_session.add(batch)
    db_session.commit()

    doc = Document(
        title="Doc",
        content="x",
        ingest_batch_id=batch.id,
        case_id=case.id,
        needs_review=False,
    )
    db_session.add(doc)
    db_session.commit()

    db_session.add(Entity(case_id=case.id, type=EntityType.PERSON, name="Alice"))
    db_session.add(
        ActionItem(
            case_id=case.id,
            title="Frist",
            action_type=ActionItemType.DEADLINE,
            due_date=datetime(2026, 6, 1),
        )
    )
    db_session.add(
        LegalCost(
            case_id=case.id,
            category=CostCategory.GERICHTSKOSTEN,
            title="Gerichtskosten 1. Instanz",
            amount_net=100,
            amount_gross=100,
            status=CostStatus.OFFEN,
        )
    )
    claim = Claim(
        claim_text="X claims Y",
        claim_type=ClaimType.FACTUAL,
    )
    db_session.add(claim)
    db_session.flush()
    db_session.add(
        ClaimEvidence(
            claim_id=claim.id, document_id=doc.id, role=ClaimEvidenceRole.ASSERTS
        )
    )
    db_session.commit()
    return case, doc, batch, claim


@pytest.mark.unit
def test_delete_and_revert_full_cascade(
    db_session, case_with_full_dependencies, monkeypatch
):
    case, doc, batch, claim = case_with_full_dependencies

    # No-op the re-enrich step in unit context — it dispatches Celery tasks.
    from app.services import case_service as cs

    monkeypatch.setattr(cs, "_reset_and_reenrich", lambda db, docs: None, raising=False)

    service = cs.CaseService(db_session)
    result = service.delete_and_revert(case.id)

    assert result is not None
    assert result["doc_count"] == 1
    assert {d.id for d in result["docs"]} == {doc.id}

    db_session.expire_all()

    # Case + dependents gone
    assert db_session.query(Case).filter(Case.id == case.id).first() is None
    assert db_session.query(Entity).filter(Entity.case_id == case.id).count() == 0
    assert (
        db_session.query(ActionItem).filter(ActionItem.case_id == case.id).count() == 0
    )
    assert db_session.query(LegalCost).filter(LegalCost.case_id == case.id).count() == 0
    # Wave 2A: claims are global. delete_and_revert preserves claims (and
    # their ClaimEvidence rows) because the documents are reverted to
    # _TRIAGE, not deleted. The claim's case scope follows the document
    # back to _TRIAGE alongside it.
    surviving_claim = db_session.get(Claim, claim.id)
    assert surviving_claim is not None
    surviving_evidence = (
        db_session.query(ClaimEvidence).filter(ClaimEvidence.claim_id == claim.id).all()
    )
    assert len(surviving_evidence) >= 1
    assert all(ev.document_id == doc.id for ev in surviving_evidence)

    # Document reverted to triage with needs_review=True
    refreshed = db_session.get(Document, doc.id)
    assert refreshed is not None
    assert refreshed.case_id == "_TRIAGE"
    assert refreshed.needs_review is True
    assert refreshed.proceeding_id is None

    # IngestBatch reverted
    refreshed_batch = db_session.get(IngestBatch, batch.id)
    assert refreshed_batch is not None
    assert refreshed_batch.case_id is None
    assert refreshed_batch.proceeding_id is None


@pytest.mark.unit
def test_delete_and_revert_missing_case_returns_none(db_session):
    from app.services.case_service import CaseService

    service = CaseService(db_session)
    assert service.delete_and_revert("DOES-NOT-EXIST") is None


@pytest.mark.unit
def test_delete_and_revert_refuses_triage(db_session):
    from app.services.case_service import CaseService

    # `_TRIAGE` is pre-seeded by the conftest cleanup_per_test fixture; no
    # need to insert it.
    service = CaseService(db_session)
    with pytest.raises(ValueError, match="Triage"):
        service.delete_and_revert("_TRIAGE")
