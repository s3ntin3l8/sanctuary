"""Second-opinion findings: every confirm path, staleness and the review reason."""

from datetime import UTC, datetime
from unittest.mock import patch

import pytest
from fastapi.testclient import TestClient

from app.main import app
from app.models.database import (
    ActionItem,
    AiReviewFinding,
    Case,
    Claim,
    ClaimEvidence,
    ClaimEvidenceProposal,
    ClaimMergeProposal,
    CostSignal,
    Document,
    DocumentRelationship,
    Entity,
    Proceeding,
    RejectedRelationship,
    User,
)
from app.models.enums import (
    ActionItemStatus,
    ActionItemType,
    CaseStatus,
    ClaimEvidenceRole,
    ClaimStatus,
    ClaimType,
    CostSignalType,
    EntityType,
    Jurisdiction,
    OriginatorType,
    PipelineStage,
    ProceedingCourtLevel,
    ProceedingStatus,
    ProposalConfidence,
    ProposalStatus,
    RelationshipConfidence,
    RelationshipType,
    ReviewerKind,
    ReviewFindingStatus,
    ReviewTargetKind,
    ReviewVerdict,
    SignificanceTier,
)
from app.services import ai_review_service
from app.services.ingestion.service import compute_review_reasons
from app.services.intelligence.document_enricher import _apply_enrichment
from app.services.pipeline_status import mark_completed

pytestmark = pytest.mark.integration

client = TestClient(app)


def _owner(db) -> User:
    return db.query(User).filter_by(email="admin@localhost").one()


def _doc(db, case_id="TEST-001", title="Letter", **kw) -> Document:
    doc = Document(
        title=title,
        case_id=case_id,
        owner_id=_owner(db).id,
        originator_type=OriginatorType.OPPOSING,
        sender="Kanzlei Weber",
        issued_date=datetime(2026, 3, 1, tzinfo=UTC),
        received_date=datetime(2026, 3, 2, tzinfo=UTC),
        significance_tier=SignificanceTier.INFORMATIONAL,
        content="Die Antragsgegnerin wird verpflichtet ...",
        **kw,
    )
    db.add(doc)
    db.commit()
    return doc


def _record(db, kind, *, verdict=ReviewVerdict.DISAGREE, proposed=None, **ident):
    target = ai_review_service.resolve_target(db, kind, **ident)
    finding = ai_review_service.record_finding(
        db,
        target,
        reviewer_kind=ReviewerKind.AGENT,
        agent_token_id=None,
        verdict=verdict,
        rationale="The letter says otherwise.",
        proposed_value=proposed,
        cited_excerpt="wird verpflichtet",
        can_edit_case=lambda _case_id: True,
    )
    db.commit()
    return finding


def _confirm(finding_id):
    return client.post(f"/api/v1/review-findings/{finding_id}/confirm")


def _claim(db, doc, text="The payment was late", status=ClaimStatus.ASSERTED):
    claim = Claim(claim_text=text, claim_type=ClaimType.FACTUAL, status=status)
    db.add(claim)
    db.flush()
    db.add(
        ClaimEvidence(
            claim_id=claim.id,
            document_id=doc.id,
            role=ClaimEvidenceRole.ASSERTS,
            confidence=RelationshipConfidence.AI_DETECTED,
        )
    )
    db.commit()
    return claim


# --- Confirm paths ----------------------------------------------------------------


def test_document_field_confirm_survives_reenrichment(db_session, sample_case):
    doc = _doc(db_session)
    finding = _record(
        db_session,
        ReviewTargetKind.DOCUMENT_FIELD,
        document_id=doc.id,
        field="significance_tier",
        proposed="critical",
    )
    assert finding.ai_value == "informational"
    assert finding.status == ReviewFindingStatus.PENDING

    resp = _confirm(finding.id)
    assert resp.status_code == 200, resp.text
    assert resp.json()["status"] == "confirmed"
    assert resp.json()["resolved_at"]

    db_session.expire_all()
    doc = db_session.get(Document, doc.id)
    assert doc.significance_tier == SignificanceTier.CRITICAL
    assert doc.extraction_confidence["significance_tier"] == "user_set"
    _apply_enrichment(doc, {"significance_tier": "administrative"}, db_session)
    assert doc.significance_tier == SignificanceTier.CRITICAL


def test_issued_date_confirm(db_session, sample_case):
    doc = _doc(db_session)
    finding = _record(
        db_session,
        ReviewTargetKind.DOCUMENT_FIELD,
        document_id=doc.id,
        field="issued_date",
        proposed="2026-02-14",
    )
    assert finding.ai_value == "2026-03-01"
    assert _confirm(finding.id).status_code == 200
    db_session.expire_all()
    assert db_session.get(Document, doc.id).issued_date.date().isoformat() == (
        "2026-02-14"
    )


def test_summary_confirm_replaces_and_approves(db_session, sample_case):
    doc = _doc(
        db_session,
        ai_summary={
            "legal_significance": "Wrong",
            "required_action": None,
            "financial_impact": None,
        },
    )
    proposed = {
        "legal_significance": "The court orders payment.",
        "required_action": "Pay by 1 April.",
        "financial_impact": "EUR 1,200",
    }
    finding = _record(
        db_session, ReviewTargetKind.SUMMARY, document_id=doc.id, proposed=proposed
    )
    assert _confirm(finding.id).status_code == 200
    db_session.expire_all()
    doc = db_session.get(Document, doc.id)
    assert doc.ai_summary == proposed
    assert doc.ai_summary_approved_at is not None


def test_case_assignment_confirm_files_the_document(db_session, sample_case):
    other = Case(
        id="OTHER-002",
        title="Other",
        status=CaseStatus.INTAKE,
        jurisdiction=Jurisdiction.DE,
    )
    db_session.add(other)
    db_session.flush()
    proc = Proceeding(
        case_id="OTHER-002",
        court_name="AG Berlin",
        court_level=ProceedingCourtLevel.AG,
        status=ProceedingStatus.ACTIVE,
    )
    db_session.add(proc)
    db_session.commit()
    doc = _doc(db_session)
    finding = _record(
        db_session,
        ReviewTargetKind.CASE_ASSIGNMENT,
        document_id=doc.id,
        proposed={"case_id": "OTHER-002", "proceeding_id": proc.id},
    )
    assert _confirm(finding.id).status_code == 200
    db_session.expire_all()
    doc = db_session.get(Document, doc.id)
    assert doc.case_id == "OTHER-002"
    assert doc.proceeding_id == proc.id
    assert doc.extraction_confidence["case_id"] == "user_set"


def test_relationship_disagreement_rejects_the_edge(db_session, sample_case):
    a = _doc(db_session, title="Reply")
    b = _doc(db_session, title="Motion")
    rel = DocumentRelationship(
        from_document_id=a.id,
        to_document_id=b.id,
        relationship_type=RelationshipType.REPLIES_TO,
        confidence=RelationshipConfidence.AI_DETECTED,
    )
    db_session.add(rel)
    db_session.commit()
    rel_id, a_id, b_id = rel.id, a.id, b.id
    finding = _record(db_session, ReviewTargetKind.RELATIONSHIP, target_id=rel_id)
    assert finding.document_id == a_id
    assert _confirm(finding.id).status_code == 200
    db_session.expire_all()
    assert db_session.query(DocumentRelationship).filter_by(id=rel_id).count() == 0
    assert (
        db_session.query(RejectedRelationship)
        .filter_by(from_document_id=a_id, to_document_id=b_id)
        .count()
        == 1
    )


def test_claim_status_and_text(db_session, sample_case):
    doc = _doc(db_session)
    claim = _claim(db_session, doc)
    status_finding = _record(
        db_session,
        ReviewTargetKind.CLAIM,
        target_id=claim.id,
        field="status",
        proposed="established",
    )
    assert _confirm(status_finding.id).status_code == 200
    db_session.expire_all()
    assert db_session.get(Claim, claim.id).status == ClaimStatus.ESTABLISHED

    text_finding = _record(
        db_session, ReviewTargetKind.CLAIM, target_id=claim.id, field="text"
    )
    assert _confirm(text_finding.id).status_code == 200
    db_session.expire_all()
    assert db_session.get(Claim, claim.id).dismissed_at is not None


def test_proposals_are_dismissed(db_session, sample_case):
    doc = _doc(db_session)
    target = _claim(db_session, _doc(db_session, title="Earlier"), "Rent unpaid")
    new = _claim(db_session, doc, "Rent was not paid")
    ev = ClaimEvidenceProposal(
        target_claim_id=target.id,
        source_document_id=doc.id,
        proposed_role=ClaimEvidenceRole.CONTESTS,
        confidence=ProposalConfidence.HIGH,
        status=ProposalStatus.PENDING,
    )
    merge = ClaimMergeProposal(
        new_claim_id=new.id,
        existing_claim_id=target.id,
        confidence=ProposalConfidence.HIGH,
        status=ProposalStatus.PENDING,
    )
    db_session.add_all([ev, merge])
    db_session.commit()
    f_ev = _record(
        db_session, ReviewTargetKind.CLAIM_EVIDENCE_PROPOSAL, target_id=ev.id
    )
    f_merge = _record(
        db_session, ReviewTargetKind.CLAIM_MERGE_PROPOSAL, target_id=merge.id
    )
    assert _confirm(f_ev.id).status_code == 200
    assert _confirm(f_merge.id).status_code == 200
    db_session.expire_all()
    assert db_session.get(ClaimEvidenceProposal, ev.id).status == (
        ProposalStatus.DISMISSED
    )
    assert db_session.get(ClaimMergeProposal, merge.id).status == (
        ProposalStatus.DISMISSED
    )


def _action_item(db, doc) -> ActionItem:
    item = ActionItem(
        case_id=doc.case_id,
        source_document_id=doc.id,
        title="Respond to motion",
        due_date=datetime(2026, 4, 1, tzinfo=UTC),
        action_type=ActionItemType.DEADLINE,
        status=ActionItemStatus.OPEN,
    )
    db.add(item)
    db.commit()
    return item


def test_action_item_due_date_and_dismissal(db_session, sample_case):
    doc = _doc(db_session)
    item = _action_item(db_session, doc)
    due = _record(
        db_session,
        ReviewTargetKind.ACTION_ITEM,
        target_id=item.id,
        field="due_date",
        proposed="2026-04-15",
    )
    assert _confirm(due.id).status_code == 200
    db_session.expire_all()
    assert db_session.get(ActionItem, item.id).due_date.date().isoformat() == (
        "2026-04-15"
    )

    no_deadline = _record(
        db_session, ReviewTargetKind.ACTION_ITEM, target_id=item.id, field="title"
    )
    assert _confirm(no_deadline.id).status_code == 200
    db_session.expire_all()
    assert db_session.get(ActionItem, item.id).status == ActionItemStatus.DISMISSED


def test_cost_signal_client_role(db_session, sample_case):
    doc = _doc(db_session)
    signal = CostSignal(
        case_id=doc.case_id,
        source_document_id=doc.id,
        signal_type=CostSignalType.COST_RULING,
        allocation={"loser": 1.0, "client_role": "loser", "auto_detected": True},
    )
    db_session.add(signal)
    db_session.commit()
    finding = _record(
        db_session,
        ReviewTargetKind.COST_SIGNAL,
        target_id=signal.id,
        field="client_role",
        proposed="winner",
    )
    assert finding.ai_value == "loser"
    assert _confirm(finding.id).status_code == 200
    db_session.expire_all()
    allocation = db_session.get(CostSignal, signal.id).allocation
    assert allocation["client_role"] == "winner"
    assert "auto_detected" not in allocation


def test_entity_confirm_only_records(db_session, sample_case):
    doc = _doc(db_session)
    entity = Entity(
        case_id=doc.case_id,
        type=EntityType.PERSON,
        name="Herr Weber",
        source_document_id=doc.id,
    )
    db_session.add(entity)
    db_session.commit()
    finding = _record(
        db_session,
        ReviewTargetKind.ENTITY,
        target_id=entity.id,
        proposed={"type": "law_firm"},
    )
    resp = _confirm(finding.id)
    assert resp.status_code == 200
    assert resp.json()["status"] == "confirmed"
    db_session.expire_all()
    assert db_session.get(Entity, entity.id).type == EntityType.PERSON


def test_ocr_text_confirm_retries_extract(db_session, sample_case):
    doc = _doc(db_session)
    finding = _record(
        db_session,
        ReviewTargetKind.OCR_TEXT,
        document_id=doc.id,
        proposed={"pages": [{"page": 1, "text": "Die Antragstellerin wird ..."}]},
    )
    with patch("app.services.stage_retry.dispatch_pipeline_retry") as dispatch:
        assert _confirm(finding.id).status_code == 200
    dispatch.assert_called_once()
    assert dispatch.call_args.args[2] == PipelineStage.EXTRACT


def test_case_brief_refresh_and_close(db_session, sample_case):
    case = db_session.get(Case, sample_case.id)
    case.ai_brief = {
        "posture": "Holding.",
        "next_move": "Wait.",
        "detected_status": "trial",
    }
    db_session.commit()
    posture = _record(
        db_session,
        ReviewTargetKind.CASE_BRIEF,
        case_id=case.id,
        field="posture",
        proposed="The ruling went against the client.",
    )
    with patch("app.tasks.dispatch.dispatch_task") as dispatch:
        assert _confirm(posture.id).status_code == 200
    dispatch.assert_called_once()

    status = _record(
        db_session,
        ReviewTargetKind.CASE_BRIEF,
        case_id=case.id,
        field="detected_status",
        proposed="closed",
    )
    assert _confirm(status.id).status_code == 200
    db_session.expire_all()
    assert db_session.get(Case, case.id).status == CaseStatus.CLOSED


# --- Lifecycle --------------------------------------------------------------------


def test_agree_is_noted_and_cannot_be_resolved(db_session, sample_case):
    doc = _doc(db_session)
    finding = _record(
        db_session,
        ReviewTargetKind.DOCUMENT_FIELD,
        verdict=ReviewVerdict.AGREE,
        document_id=doc.id,
        field="sender",
    )
    assert finding.status == ReviewFindingStatus.NOTED
    resp = _confirm(finding.id)
    assert resp.status_code == 409
    assert resp.json()["code"] == "not_pending"


def test_dismiss_leaves_the_output(db_session, sample_case):
    doc = _doc(db_session)
    finding = _record(
        db_session,
        ReviewTargetKind.DOCUMENT_FIELD,
        document_id=doc.id,
        field="sender",
        proposed="Amtsgericht Berlin",
    )
    resp = client.post(f"/api/v1/review-findings/{finding.id}/dismiss")
    assert resp.status_code == 200
    assert resp.json()["status"] == "dismissed"
    db_session.expire_all()
    assert db_session.get(Document, doc.id).sender == "Kanzlei Weber"


def test_repeated_review_updates_the_pending_finding(db_session, sample_case):
    doc = _doc(db_session)
    first = _record(
        db_session,
        ReviewTargetKind.DOCUMENT_FIELD,
        document_id=doc.id,
        field="significance_tier",
        proposed="critical",
    )
    second = _record(
        db_session,
        ReviewTargetKind.DOCUMENT_FIELD,
        document_id=doc.id,
        field="significance_tier",
        proposed="significant",
    )
    assert second.id == first.id
    assert second.proposed_value == "significant"
    assert db_session.query(AiReviewFinding).count() == 1


def test_stage_rerun_that_changes_the_value_marks_stale(db_session, sample_case):
    doc = _doc(db_session)
    unchanged = _record(
        db_session,
        ReviewTargetKind.DOCUMENT_FIELD,
        document_id=doc.id,
        field="sender",
        proposed="Amtsgericht Berlin",
    )
    changed = _record(
        db_session,
        ReviewTargetKind.DOCUMENT_FIELD,
        document_id=doc.id,
        field="significance_tier",
        proposed="critical",
    )
    # ENRICH re-runs and writes another tier.
    doc = db_session.get(Document, doc.id)
    doc.significance_tier = SignificanceTier.SIGNIFICANT
    db_session.commit()
    mark_completed(doc.id, PipelineStage.ENRICH, db_session)

    db_session.expire_all()
    assert db_session.get(AiReviewFinding, changed.id).status == (
        ReviewFindingStatus.STALE
    )
    assert db_session.get(AiReviewFinding, unchanged.id).status == (
        ReviewFindingStatus.PENDING
    )
    resp = _confirm(changed.id)
    assert resp.status_code == 409


def test_brief_change_marks_brief_findings_stale(db_session, sample_case):
    case = db_session.get(Case, sample_case.id)
    case.ai_brief = {"posture": "Holding.", "next_move": "Wait."}
    db_session.commit()
    finding = _record(
        db_session,
        ReviewTargetKind.CASE_BRIEF,
        case_id=case.id,
        field="posture",
        proposed="Losing.",
    )
    case.ai_brief = {"posture": "Changed.", "next_move": "Wait."}
    db_session.commit()
    ai_review_service.mark_stale(db_session, case_id=case.id)
    db_session.commit()
    db_session.expire_all()
    assert db_session.get(AiReviewFinding, finding.id).status == (
        ReviewFindingStatus.STALE
    )


def test_review_reason_appears_and_clears(db_session, sample_case):
    doc = _doc(db_session)
    assert "ai_review_disagreement" not in compute_review_reasons(doc)
    finding = _record(
        db_session,
        ReviewTargetKind.DOCUMENT_FIELD,
        document_id=doc.id,
        field="sender",
        proposed="Amtsgericht Berlin",
    )
    db_session.expire_all()
    assert "ai_review_disagreement" in db_session.get(Document, doc.id).review_reasons

    assert client.post(f"/api/v1/review-findings/{finding.id}/dismiss").status_code == (
        200
    )
    db_session.expire_all()
    assert "ai_review_disagreement" not in (
        db_session.get(Document, doc.id).review_reasons
    )


def test_listing_by_document_and_case(db_session, sample_case):
    doc = _doc(db_session)
    _record(
        db_session,
        ReviewTargetKind.DOCUMENT_FIELD,
        document_id=doc.id,
        field="sender",
        proposed="Amtsgericht Berlin",
    )
    by_doc = client.get(f"/api/v1/documents/{doc.id}/review-findings").json()
    assert [f["target_label"] for f in by_doc] == ["Sender"]
    assert by_doc[0]["ai_value"] == "Kanzlei Weber"
    by_case = client.get(
        f"/api/v1/cases/{sample_case.id}/review-findings?status=pending"
    ).json()
    assert [f["id"] for f in by_case] == [by_doc[0]["id"]]


@pytest.mark.parametrize(
    ("field", "proposed"),
    [
        ("significance_tier", "urgent"),
        ("document_type", "memo"),
        ("originator_type", "alien"),
        ("issued_date", "1. März"),
    ],
)
def test_invalid_proposed_values_are_refused(db_session, sample_case, field, proposed):
    doc = _doc(db_session)
    with pytest.raises(ai_review_service.ReviewFindingError) as err:
        _record(
            db_session,
            ReviewTargetKind.DOCUMENT_FIELD,
            document_id=doc.id,
            field=field,
            proposed=proposed,
        )
    assert err.value.status == 422


def test_only_editors_resolve(db_session, sample_case, auth_enabled):
    from app.services import auth_service

    auth_service.create_user(
        db_session,
        email="viewer@example.com",
        password="password123",  # pragma: allowlist secret
    )
    db_session.commit()
    doc = _doc(db_session)
    finding = _record(
        db_session,
        ReviewTargetKind.DOCUMENT_FIELD,
        document_id=doc.id,
        field="sender",
        proposed="Amtsgericht Berlin",
    )
    outsider = TestClient(app, follow_redirects=False)
    outsider.post(
        "/api/v1/auth/login",
        json={
            "email": "viewer@example.com",
            "password": "password123",  # pragma: allowlist secret
        },
    )
    assert outsider.post(
        f"/api/v1/review-findings/{finding.id}/confirm"
    ).status_code == (404)
    assert outsider.get(f"/api/v1/documents/{doc.id}/review-findings").status_code == (
        404
    )


def test_case_assignment_out_of_a_draft_keeps_the_finding(db_session, sample_case):
    draft = Case(
        id="DRAFT-9",
        title="AI draft",
        status=CaseStatus.INTAKE,
        jurisdiction=Jurisdiction.DE,
        is_draft=True,
    )
    db_session.add(draft)
    db_session.commit()
    doc = _doc(db_session, case_id="DRAFT-9")
    finding = _record(
        db_session,
        ReviewTargetKind.CASE_ASSIGNMENT,
        document_id=doc.id,
        proposed={"case_id": sample_case.id},
    )
    resp = _confirm(finding.id)
    assert resp.status_code == 200, resp.text
    assert resp.json()["status"] == "confirmed"
    db_session.expire_all()
    assert db_session.get(Case, "DRAFT-9") is None
    kept = db_session.get(AiReviewFinding, finding.id)
    assert kept.case_id == sample_case.id


def test_stage_completed_through_a_stale_session_still_marks_stale(
    db_session, db_session_factory, sample_case
):
    doc = _doc(db_session)
    finding = _record(
        db_session,
        ReviewTargetKind.DOCUMENT_FIELD,
        document_id=doc.id,
        field="significance_tier",
        proposed="critical",
    )
    completing = db_session_factory()
    try:
        assert completing.get(Document, doc.id).significance_tier == (
            SignificanceTier.INFORMATIONAL
        )
        writer = db_session_factory()
        try:
            writer.get(
                Document, doc.id
            ).significance_tier = SignificanceTier.SIGNIFICANT
            writer.commit()
        finally:
            writer.close()
        mark_completed(doc.id, PipelineStage.ENRICH, completing)
    finally:
        completing.close()
    db_session.expire_all()
    assert db_session.get(AiReviewFinding, finding.id).status == (
        ReviewFindingStatus.STALE
    )
