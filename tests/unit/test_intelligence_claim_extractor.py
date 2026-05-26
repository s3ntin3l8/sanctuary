"""Tests for Phase 4c claim extractor."""

from datetime import datetime, timedelta
from unittest.mock import patch

import pytest

from app.models.database import Claim, ClaimEvidence, Document
from app.models.enums import (
    ClaimEvidenceRole,
    ClaimStatus,
    ClaimType,
    OriginatorType,
    RelationshipConfidence,
    SignificanceTier,
)


def _make_claim(
    db_session,
    *,
    asserting_doc,
    claim_text: str,
    claim_type: ClaimType = ClaimType.FACTUAL,
    status: ClaimStatus = ClaimStatus.ASSERTED,
    asserts_ingest_date: datetime | None = None,
) -> Claim:
    """Wave 2A test helper: create a global Claim plus its canonical ASSERTS
    evidence row. Replaces the old `Claim(case_id=…, source_document_id=…)`
    pattern that's invalid after the column-drop migration.

    `asserts_ingest_date` lets retry-cleanup tests backdate the ASSERTS row
    past the claim_extractor.extract() debounce window so the cleanup logic
    is actually exercised. Default is the model's _utcnow (i.e. "just now"),
    which is what most tests want.
    """
    claim = Claim(
        claim_text=claim_text,
        claim_type=claim_type,
        status=status,
        first_made_at=datetime.now(),
        last_updated_at=datetime.now(),
    )
    db_session.add(claim)
    db_session.flush()
    ev_kwargs = {
        "claim_id": claim.id,
        "document_id": asserting_doc.id,
        "role": ClaimEvidenceRole.ASSERTS,
    }
    if asserts_ingest_date is not None:
        ev_kwargs["ingest_date"] = asserts_ingest_date
    db_session.add(ClaimEvidence(**ev_kwargs))
    return claim


# Backdate prior ASSERTS rows past the 300s debounce window so the
# stale-cleanup branch is actually entered (otherwise extract() short-circuits
# with reason="recent_extraction" — which is the intended behaviour for
# dispatch-race scenarios but defeats tests that want to exercise cleanup).
_PRE_DEBOUNCE = datetime.now() - timedelta(hours=1)


@pytest.fixture
def significant_doc(db_session, sample_case):
    doc = Document(
        title="Klageerwiderung",
        content="Die Beklagte widerspricht der Klage. Sie bestreitet, an jenem Tag am Ort gewesen zu sein.",
        case_id=sample_case.id,
        significance_tier=SignificanceTier.SIGNIFICANT,
        originator_type=OriginatorType.OWN,
        ai_summary={
            "legal_significance": "Defense response",
            "required_action": "none",
            "financial_impact": "none",
        },
    )
    db_session.add(doc)
    db_session.commit()
    db_session.refresh(doc)
    return doc


@pytest.fixture
def existing_claim(db_session, sample_case, significant_doc):
    """A pre-existing claim owned by a *different* document. Cross-doc evidence
    links from `significant_doc` point at this claim — the realistic shape.

    (If the claim's source were `significant_doc` itself, retry-cleanup would
    delete it before the AI's evidence_link could fire, which doesn't reflect
    how the AI actually uses cross-doc evidence in production.)
    """
    other_doc = Document(
        title="Prior filing",
        content="Prior content asserting a fact.",
        case_id=sample_case.id,
        significance_tier=SignificanceTier.SIGNIFICANT,
    )
    db_session.add(other_doc)
    db_session.flush()
    claim = _make_claim(
        db_session,
        asserting_doc=other_doc,
        claim_text="Defendant was present at location on 2024-01-10",
    )
    db_session.commit()
    db_session.refresh(claim)
    return claim


@pytest.mark.unit
def test_new_claims_created(db_session, significant_doc, sample_case):
    ai_result = {
        "new_claims": [
            {
                "claim_text": "The defendant was not present at the hearing on 15.03.2026",
                "claim_type": "factual",
                "excerpt": "She denies being there",
            },
            {
                "claim_text": "The contract was validly executed under § 433 BGB on 01.01.2024",
                "claim_type": "legal",
                "excerpt": "The contract was signed",
            },
        ],
        "evidence_links": [],
    }

    with (
        patch(
            "app.services.intelligence.claim_extractor.SessionLocal",
            return_value=db_session,
        ),
        patch.object(db_session, "close"),
        patch(
            "app.services.intelligence.claim_extractor._call_claim_extractor_sync",
            return_value=ai_result,
        ),
    ):
        from app.services.intelligence.claim_extractor import extract

        extract(significant_doc.id)

    from app.repositories.claim import ClaimRepository

    claims = list(ClaimRepository(db_session).claims_for_case(sample_case.id))
    assert len(claims) == 2
    claim_texts = {c.claim_text for c in claims}
    assert "The defendant was not present at the hearing on 15.03.2026" in claim_texts
    assert (
        "The contract was validly executed under § 433 BGB on 01.01.2024" in claim_texts
    )

    # Source doc ASSERTS its own new claims (the canonical "originated by"
    # evidence row added in the Sharpen-Claims plan).
    evidence = db_session.query(ClaimEvidence).all()
    assert all(e.role == ClaimEvidenceRole.ASSERTS for e in evidence)
    assert all(e.document_id == significant_doc.id for e in evidence)
    assert all(e.confidence == RelationshipConfidence.AI_DETECTED for e in evidence)


@pytest.mark.unit
def test_court_doc_claims_arrive_established(db_session, sample_case):
    """Wave 1 invariant: claims from COURT-originator documents arrive with
    status=ESTABLISHED (procedural reality), not the default ASSERTED."""
    court_doc = Document(
        title="Beschluss LG Ingolstadt",
        content="Die Beschwerde wird zurückgewiesen.",
        case_id=sample_case.id,
        significance_tier=SignificanceTier.CRITICAL,
        originator_type=OriginatorType.COURT,
        ai_summary={
            "legal_significance": "Court ruling",
            "required_action": "none",
            "financial_impact": "none",
        },
    )
    db_session.add(court_doc)
    db_session.commit()
    db_session.refresh(court_doc)

    ai_result = {
        "new_claims": [
            {
                "claim_text": "The court rejected the complaint against the order",
                "claim_type": "procedural",
                "excerpt": "Die Beschwerde wird zurückgewiesen",
            }
        ],
        "evidence_links": [],
    }

    with (
        patch(
            "app.services.intelligence.claim_extractor.SessionLocal",
            return_value=db_session,
        ),
        patch.object(db_session, "close"),
        patch(
            "app.services.intelligence.claim_extractor._call_claim_extractor_sync",
            return_value=ai_result,
        ),
    ):
        from app.services.intelligence.claim_extractor import extract

        extract(court_doc.id)

    from app.repositories.claim import ClaimRepository

    claims = list(ClaimRepository(db_session).claims_asserted_by_document(court_doc.id))
    assert len(claims) == 1
    assert claims[0].status == ClaimStatus.ESTABLISHED, (
        "court-originated claim must arrive ESTABLISHED, not ASSERTED"
    )


@pytest.mark.unit
def test_non_court_doc_claims_arrive_asserted(db_session, significant_doc):
    """Mirror invariant: non-COURT documents (significant_doc fixture is OWN)
    keep the default ASSERTED status — only court findings short-circuit the
    truth-status workflow."""
    ai_result = {
        "new_claims": [
            {
                "claim_text": "The contract was signed under § 433 BGB on 01.01.2024",
                "claim_type": "legal",
                "excerpt": "Vertrag",
            }
        ],
        "evidence_links": [],
    }

    with (
        patch(
            "app.services.intelligence.claim_extractor.SessionLocal",
            return_value=db_session,
        ),
        patch.object(db_session, "close"),
        patch(
            "app.services.intelligence.claim_extractor._call_claim_extractor_sync",
            return_value=ai_result,
        ),
    ):
        from app.services.intelligence.claim_extractor import extract

        extract(significant_doc.id)

    from app.repositories.claim import ClaimRepository

    claims = list(
        ClaimRepository(db_session).claims_asserted_by_document(significant_doc.id)
    )
    assert len(claims) == 1
    assert claims[0].status == ClaimStatus.ASSERTED


@pytest.mark.unit
def test_court_relay_doc_claims_arrive_asserted(db_session, sample_case):
    """A court RELAY document (sender=court but it's just forwarding a
    party submission) carries the party's claims, not the court's. Its
    claims must arrive ASSERTED, not ESTABLISHED — even though
    `originator_type == COURT`."""
    relay_doc = Document(
        title="Zustellung durch das Gericht",
        content="Die Antragstellerin trägt vor: Die Wohnung wurde am 12.03.2024 übergeben.",
        case_id=sample_case.id,
        significance_tier=SignificanceTier.SIGNIFICANT,
        originator_type=OriginatorType.COURT,
        court_relay=True,
        ai_summary={
            "legal_significance": "Forwarded submission",
            "required_action": "none",
            "financial_impact": "none",
        },
    )
    db_session.add(relay_doc)
    db_session.commit()
    db_session.refresh(relay_doc)

    ai_result = {
        "new_claims": [
            {
                "claim_text": "The apartment was handed over on 2024-03-12",
                "claim_type": "factual",
                "excerpt": "Die Wohnung wurde am 12.03.2024 übergeben",
            }
        ],
        "evidence_links": [],
    }

    with (
        patch(
            "app.services.intelligence.claim_extractor.SessionLocal",
            return_value=db_session,
        ),
        patch.object(db_session, "close"),
        patch(
            "app.services.intelligence.claim_extractor._call_claim_extractor_sync",
            return_value=ai_result,
        ),
    ):
        from app.services.intelligence.claim_extractor import extract

        extract(relay_doc.id)

    from app.repositories.claim import ClaimRepository

    claims = list(ClaimRepository(db_session).claims_asserted_by_document(relay_doc.id))
    assert len(claims) == 1
    assert claims[0].status == ClaimStatus.ASSERTED, (
        "court-relay doc must not produce ESTABLISHED claims — it's forwarding "
        "a party submission, not making a court finding"
    )


@pytest.mark.unit
def test_attributed_originator_overrides_court_originator(db_session, sample_case):
    """When `attributed_originator` names the true author as non-court
    (e.g. "opposing"), claims must arrive ASSERTED even if the bare
    `originator_type` is COURT (a common misclassification path)."""
    misclassified_doc = Document(
        title="Schriftsatz",
        content="Die Beklagtenseite bestreitet die Übergabe und behauptet die Wohnung sei mangelhaft.",
        case_id=sample_case.id,
        significance_tier=SignificanceTier.SIGNIFICANT,
        originator_type=OriginatorType.COURT,
        attributed_originator="opposing",
        ai_summary={
            "legal_significance": "Opposing submission",
            "required_action": "none",
            "financial_impact": "none",
        },
    )
    db_session.add(misclassified_doc)
    db_session.commit()
    db_session.refresh(misclassified_doc)

    ai_result = {
        "new_claims": [
            {
                "claim_text": "The apartment was defective at handover and unfit for tenancy",
                "claim_type": "factual",
                "excerpt": "die Wohnung sei mangelhaft",
            }
        ],
        "evidence_links": [],
    }

    with (
        patch(
            "app.services.intelligence.claim_extractor.SessionLocal",
            return_value=db_session,
        ),
        patch.object(db_session, "close"),
        patch(
            "app.services.intelligence.claim_extractor._call_claim_extractor_sync",
            return_value=ai_result,
        ),
    ):
        from app.services.intelligence.claim_extractor import extract

        extract(misclassified_doc.id)

    from app.repositories.claim import ClaimRepository

    claims = list(
        ClaimRepository(db_session).claims_asserted_by_document(misclassified_doc.id)
    )
    assert len(claims) == 1
    assert claims[0].status == ClaimStatus.ASSERTED, (
        "attributed_originator='opposing' must override originator_type=COURT — "
        "those are party claims, not procedural reality"
    )


@pytest.mark.unit
def test_established_claim_not_auto_refuted(db_session, significant_doc, sample_case):
    """Wave 1 invariant: an ESTABLISHED claim (typically a court finding) is
    not auto-flipped to REFUTED when a later document takes a refutes stance.
    The procedural reality holds until appellate review changes it explicitly.

    Note: ESTABLISHED claims are not normally in `get_open_in_case`'s candidate
    set (which filters to ASSERTED+CONTESTED), so the in-extractor guard is
    defensive layering. We simulate the candidate list directly to exercise it.
    """
    other_doc = Document(
        title="Earlier court ruling",
        content="Established by the court.",
        case_id=sample_case.id,
        significance_tier=SignificanceTier.CRITICAL,
        originator_type=OriginatorType.COURT,
    )
    db_session.add(other_doc)
    db_session.flush()

    established_claim = _make_claim(
        db_session,
        asserting_doc=other_doc,
        claim_text="The court found that suspension under § 180 III ZVG is not warranted",
        claim_type=ClaimType.LEGAL,
        status=ClaimStatus.ESTABLISHED,
    )
    db_session.commit()
    db_session.refresh(established_claim)

    ai_result = {
        "new_claims": [],
        "evidence_links": [
            {
                "claim_id": established_claim.id,
                "role": "refutes",
                "excerpt": "We dispute this finding",
            }
        ],
    }

    # Force the candidate list to include the ESTABLISHED claim so the
    # in-extractor guard is the thing being tested.
    with (
        patch(
            "app.services.intelligence.claim_extractor.SessionLocal",
            return_value=db_session,
        ),
        patch.object(db_session, "close"),
        patch(
            "app.services.intelligence.claim_extractor._call_claim_extractor_sync",
            return_value=ai_result,
        ),
        patch(
            "app.repositories.claim.ClaimRepository.get_open_in_case",
            return_value=[established_claim],
        ),
    ):
        from app.services.intelligence.claim_extractor import extract

        extract(significant_doc.id)

    db_session.expire_all()
    survivor = db_session.get(Claim, established_claim.id)
    assert survivor.status == ClaimStatus.ESTABLISHED, (
        "ESTABLISHED claim must hold against a REFUTES proposal; the user-"
        "confirmation gate (Wave 2B) is the only path to a status change."
    )
    # Wave 2B: evidence_links produce ClaimEvidenceProposal rows, not direct
    # ClaimEvidence rows. The proposal records the REFUTES intent for audit;
    # status only changes when the user confirms (and the confirm path
    # respects the ESTABLISHED guard).
    from app.models.database import ClaimEvidenceProposal
    from app.models.enums import ProposalStatus

    refutes_proposals = (
        db_session.query(ClaimEvidenceProposal)
        .filter(
            ClaimEvidenceProposal.target_claim_id == established_claim.id,
            ClaimEvidenceProposal.proposed_role == ClaimEvidenceRole.REFUTES,
            ClaimEvidenceProposal.status == ProposalStatus.PENDING,
        )
        .all()
    )
    assert len(refutes_proposals) == 1
    # No ClaimEvidence row was written — that's the whole point.
    refutes_evidence = (
        db_session.query(ClaimEvidence)
        .filter(
            ClaimEvidence.claim_id == established_claim.id,
            ClaimEvidence.role == ClaimEvidenceRole.REFUTES,
        )
        .all()
    )
    assert len(refutes_evidence) == 0


@pytest.mark.unit
def test_evidence_link_contests_creates_proposal_and_confirm_flips_status(
    db_session, significant_doc, existing_claim
):
    """Wave 2B: evidence_links from the extractor become pending
    ClaimEvidenceProposal rows; the user's confirm action is what writes
    the ClaimEvidence row and (where applicable) flips claim status."""
    from app.models.database import ClaimEvidenceProposal
    from app.models.enums import ProposalStatus
    from app.services.claim_proposal_service import confirm_evidence

    ai_result = {
        "new_claims": [],
        "evidence_links": [
            {
                "claim_id": existing_claim.id,
                "role": "contests",
                "excerpt": "The defendant denies this",
            }
        ],
    }

    with (
        patch(
            "app.services.intelligence.claim_extractor.SessionLocal",
            return_value=db_session,
        ),
        patch.object(db_session, "close"),
        patch(
            "app.services.intelligence.claim_extractor._call_claim_extractor_sync",
            return_value=ai_result,
        ),
    ):
        from app.services.intelligence.claim_extractor import extract

        extract(significant_doc.id)

    db_session.expire_all()

    # 1) Status hasn't changed yet — proposal is pending.
    pre_confirm = db_session.get(Claim, existing_claim.id)
    assert pre_confirm.status == ClaimStatus.ASSERTED

    # 2) A pending proposal exists.
    proposal = (
        db_session.query(ClaimEvidenceProposal)
        .filter(
            ClaimEvidenceProposal.target_claim_id == existing_claim.id,
            ClaimEvidenceProposal.source_document_id == significant_doc.id,
            ClaimEvidenceProposal.proposed_role == ClaimEvidenceRole.CONTESTS,
            ClaimEvidenceProposal.status == ProposalStatus.PENDING,
        )
        .first()
    )
    assert proposal is not None

    # 3) User confirms — evidence row written, status flipped.
    confirm_evidence(proposal.id, db_session)
    db_session.commit()
    db_session.expire_all()

    post = db_session.get(Claim, existing_claim.id)
    assert post.status == ClaimStatus.CONTESTED

    evidence_from_significant = (
        db_session.query(ClaimEvidence)
        .filter(
            ClaimEvidence.claim_id == existing_claim.id,
            ClaimEvidence.document_id == significant_doc.id,
            ClaimEvidence.role == ClaimEvidenceRole.CONTESTS,
        )
        .all()
    )
    assert len(evidence_from_significant) == 1


@pytest.mark.unit
def test_evidence_link_refutes_creates_proposal_and_confirm_flips_status(
    db_session, significant_doc, existing_claim
):
    """Wave 2B: same proposal-then-confirm flow for the refutes role."""
    from app.models.database import ClaimEvidenceProposal
    from app.models.enums import ProposalStatus
    from app.services.claim_proposal_service import confirm_evidence

    ai_result = {
        "new_claims": [],
        "evidence_links": [
            {
                "claim_id": existing_claim.id,
                "role": "refutes",
                "excerpt": "Proven false by evidence",
            }
        ],
    }

    with (
        patch(
            "app.services.intelligence.claim_extractor.SessionLocal",
            return_value=db_session,
        ),
        patch.object(db_session, "close"),
        patch(
            "app.services.intelligence.claim_extractor._call_claim_extractor_sync",
            return_value=ai_result,
        ),
    ):
        from app.services.intelligence.claim_extractor import extract

        extract(significant_doc.id)

    db_session.expire_all()
    pre_confirm = db_session.get(Claim, existing_claim.id)
    assert pre_confirm.status == ClaimStatus.ASSERTED  # not flipped yet

    proposal = (
        db_session.query(ClaimEvidenceProposal)
        .filter(
            ClaimEvidenceProposal.target_claim_id == existing_claim.id,
            ClaimEvidenceProposal.proposed_role == ClaimEvidenceRole.REFUTES,
            ClaimEvidenceProposal.status == ProposalStatus.PENDING,
        )
        .first()
    )
    assert proposal is not None

    confirm_evidence(proposal.id, db_session)
    db_session.commit()
    db_session.expire_all()

    updated = db_session.get(Claim, existing_claim.id)
    assert updated.status == ClaimStatus.REFUTED


@pytest.mark.unit
def test_hallucination_guard_drops_invalid_claim_id(
    db_session, significant_doc, existing_claim
):
    ai_result = {
        "new_claims": [],
        "evidence_links": [
            {"claim_id": 99999, "role": "supports", "excerpt": "Invented claim ID"},
            {"claim_id": existing_claim.id, "role": "supports", "excerpt": "Valid"},
        ],
    }

    with (
        patch(
            "app.services.intelligence.claim_extractor.SessionLocal",
            return_value=db_session,
        ),
        patch.object(db_session, "close"),
        patch(
            "app.services.intelligence.claim_extractor._call_claim_extractor_sync",
            return_value=ai_result,
        ),
    ):
        from app.services.intelligence.claim_extractor import extract

        extract(significant_doc.id)

    # Wave 2B: only the valid evidence_link becomes a pending proposal.
    # The hallucinated claim_id=99999 is rejected at the candidates filter
    # before any proposal is written.
    from app.models.database import ClaimEvidenceProposal
    from app.models.enums import ProposalStatus

    proposals = (
        db_session.query(ClaimEvidenceProposal)
        .filter(
            ClaimEvidenceProposal.source_document_id == significant_doc.id,
            ClaimEvidenceProposal.status == ProposalStatus.PENDING,
        )
        .all()
    )
    assert len(proposals) == 1
    assert proposals[0].target_claim_id == existing_claim.id

    # No raw ClaimEvidence rows on the significant_doc side — proposals
    # don't write evidence directly.
    evidence = (
        db_session.query(ClaimEvidence)
        .filter(
            ClaimEvidence.claim_id == existing_claim.id,
            ClaimEvidence.document_id == significant_doc.id,
        )
        .all()
    )
    assert len(evidence) == 0


@pytest.mark.unit
def test_hallucination_guard_drops_invalid_role(
    db_session, significant_doc, existing_claim
):
    ai_result = {
        "new_claims": [],
        "evidence_links": [
            {"claim_id": existing_claim.id, "role": "invalidrole", "excerpt": "test"},
        ],
    }

    with (
        patch(
            "app.services.intelligence.claim_extractor.SessionLocal",
            return_value=db_session,
        ),
        patch.object(db_session, "close"),
        patch(
            "app.services.intelligence.claim_extractor._call_claim_extractor_sync",
            return_value=ai_result,
        ),
    ):
        from app.services.intelligence.claim_extractor import extract

        extract(significant_doc.id)

    # No evidence on the significant_doc side (the invalid role got dropped).
    # The pre-existing ASSERTS row on existing_claim is unrelated.
    evidence = (
        db_session.query(ClaimEvidence)
        .filter(ClaimEvidence.document_id == significant_doc.id)
        .all()
    )
    assert len(evidence) == 0


@pytest.mark.unit
def test_hallucination_guard_drops_invalid_claim_type(db_session, significant_doc):
    ai_result = {
        "new_claims": [
            {
                "claim_text": "Something asserted",
                "claim_type": "unknowntype",
                "excerpt": "test",
            },
        ],
        "evidence_links": [],
    }

    with (
        patch(
            "app.services.intelligence.claim_extractor.SessionLocal",
            return_value=db_session,
        ),
        patch.object(db_session, "close"),
        patch(
            "app.services.intelligence.claim_extractor._call_claim_extractor_sync",
            return_value=ai_result,
        ),
    ):
        from app.services.intelligence.claim_extractor import extract

        extract(significant_doc.id)

    from app.repositories.claim import ClaimRepository

    claims = list(ClaimRepository(db_session).claims_for_case(significant_doc.case_id))
    assert len(claims) == 0


@pytest.mark.unit
def test_skips_administrative_tier(db_session, sample_case):
    doc = Document(
        title="Empfangsbestätigung",
        content="Wir bestätigen den Eingang.",
        case_id=sample_case.id,
        significance_tier=SignificanceTier.ADMINISTRATIVE,
        originator_type=OriginatorType.COURT,
    )
    db_session.add(doc)
    db_session.commit()
    db_session.refresh(doc)

    with (
        patch(
            "app.services.intelligence.claim_extractor.SessionLocal",
            return_value=db_session,
        ),
        patch.object(db_session, "close"),
        patch(
            "app.services.intelligence.claim_extractor._call_claim_extractor_sync"
        ) as mock_call,
    ):
        from app.services.intelligence.claim_extractor import extract

        extract(doc.id)
        mock_call.assert_not_called()


@pytest.mark.unit
def test_relay_doc_skipped(db_session, sample_case):
    """Cover letters (document_type=RELAY) yield only letterhead/metadata
    'claims' that the prompt explicitly prohibits. Skip the AI call entirely."""
    from app.models.enums import DocumentType

    doc = Document(
        title="Begleitschreiben Amtsgericht Hamburg",
        content="Anbei übersende ich Ihnen den Beschluss zur Kenntnisnahme.",
        case_id=sample_case.id,
        significance_tier=SignificanceTier.SIGNIFICANT,  # tier passes the gate
        document_type=DocumentType.RELAY,  # but doc_type blocks
        originator_type=OriginatorType.COURT,
    )
    db_session.add(doc)
    db_session.commit()
    db_session.refresh(doc)

    with (
        patch(
            "app.services.intelligence.claim_extractor.SessionLocal",
            return_value=db_session,
        ),
        patch.object(db_session, "close"),
        patch(
            "app.services.intelligence.claim_extractor._call_claim_extractor_sync"
        ) as mock_call,
    ):
        from app.services.intelligence.claim_extractor import extract

        result = extract(doc.id)
        mock_call.assert_not_called()
        assert result == "document_type:relay"


@pytest.mark.unit
def test_retry_clears_stale_asserted_claims(db_session, significant_doc, sample_case):
    """A repeat extraction on the same doc must not accumulate claims —
    delete prior auto-extracted ASSERTED claims before re-running.

    Prior ASSERTS are backdated past _PRE_DEBOUNCE so extract()'s debounce
    short-circuit doesn't fire — we want to exercise the stale-cleanup branch.
    """
    from app.repositories.claim import ClaimRepository

    # Seed three claims from a prior run, all in default ASSERTED state.
    for i in range(3):
        _make_claim(
            db_session,
            asserting_doc=significant_doc,
            claim_text=f"Stale claim from prior run number {i} that is long enough",
            asserts_ingest_date=_PRE_DEBOUNCE,
        )
    db_session.commit()
    assert (
        len(ClaimRepository(db_session).claims_asserted_by_document(significant_doc.id))
        == 3
    )

    ai_result = {
        "new_claims": [
            {
                "claim_text": "The new run produced this single fresh claim text",
                "claim_type": "factual",
                "excerpt": "fresh excerpt",
            }
        ],
        "evidence_links": [],
    }

    with (
        patch(
            "app.services.intelligence.claim_extractor.SessionLocal",
            return_value=db_session,
        ),
        patch.object(db_session, "close"),
        patch(
            "app.services.intelligence.claim_extractor._call_claim_extractor_sync",
            return_value=ai_result,
        ),
    ):
        from app.services.intelligence.claim_extractor import extract

        extract(significant_doc.id)

    from app.repositories.claim import ClaimRepository

    db_session.expire_all()
    remaining = list(
        ClaimRepository(db_session).claims_asserted_by_document(significant_doc.id)
    )
    # Only the fresh claim from the latest run survives.
    assert len(remaining) == 1
    assert (
        remaining[0].claim_text == "The new run produced this single fresh claim text"
    )


@pytest.mark.unit
def test_retry_preserves_claims_with_cross_doc_evidence(
    db_session, significant_doc, sample_case
):
    """ib-0033 #98 regression contract: cleanup discriminator is cross-doc
    evidence presence, not status. A claim survives retry when ANY other
    document has confirmed evidence on it (SUPPORTS/CONTESTS/REFUTES/etc.);
    otherwise it's treated as auto-extracted from this document alone and
    cleaned, regardless of how its status was set."""
    # Another doc whose evidence will protect a claim from cleanup.
    other_doc = Document(
        title="Other doc that weighs in",
        content="content",
        case_id=sample_case.id,
        significance_tier=SignificanceTier.SIGNIFICANT,
    )
    db_session.add(other_doc)
    db_session.flush()

    # 1) ASSERTED with no cross-doc evidence — deleted.
    only_doc_asserted = _make_claim(
        db_session,
        asserting_doc=significant_doc,
        claim_text="Stale ASSERTED claim with no other-doc evidence",
        status=ClaimStatus.ASSERTED,
        asserts_ingest_date=_PRE_DEBOUNCE,
    )
    # 2) ESTABLISHED with no cross-doc evidence — also deleted (this is the
    #    ib-0033 #98 case: originator flipped court→opposing, so the original
    #    ESTABLISHED status is no longer valid and no other doc is propping
    #    the claim up).
    only_doc_established = _make_claim(
        db_session,
        asserting_doc=significant_doc,
        claim_text="Stale ESTABLISHED claim with no other-doc evidence",
        status=ClaimStatus.ESTABLISHED,
        asserts_ingest_date=_PRE_DEBOUNCE,
    )
    # 3) CONTESTED with cross-doc evidence from other_doc — preserved.
    contested_with_signal = _make_claim(
        db_session,
        asserting_doc=significant_doc,
        claim_text="A contested claim that another doc challenges",
        status=ClaimStatus.CONTESTED,
        asserts_ingest_date=_PRE_DEBOUNCE,
    )
    db_session.add(
        ClaimEvidence(
            claim_id=contested_with_signal.id,
            document_id=other_doc.id,
            role=ClaimEvidenceRole.CONTESTS,
            confidence=RelationshipConfidence.AI_DETECTED,
        )
    )
    db_session.commit()

    ai_result = {"new_claims": [], "evidence_links": []}

    with (
        patch(
            "app.services.intelligence.claim_extractor.SessionLocal",
            return_value=db_session,
        ),
        patch.object(db_session, "close"),
        patch(
            "app.services.intelligence.claim_extractor._call_claim_extractor_sync",
            return_value=ai_result,
        ),
    ):
        from app.services.intelligence.claim_extractor import extract

        extract(significant_doc.id)

    db_session.expire_all()
    assert db_session.get(Claim, only_doc_asserted.id) is None
    assert db_session.get(Claim, only_doc_established.id) is None, (
        "ESTABLISHED claim with no cross-doc evidence must be cleaned — "
        "this is the ib-0033 #98 case where originator flipped"
    )
    assert db_session.get(Claim, contested_with_signal.id) is not None, (
        "claim with cross-doc evidence must survive regardless of status"
    )


@pytest.mark.unit
def test_retry_preserves_evidence_pointing_at_other_docs_claims(
    db_session, significant_doc, sample_case
):
    """When this doc's claims are cleared on retry, claim_evidence rows that
    point at OTHER docs' claims (where this doc supports/contests them) must
    be preserved — they're independent of this doc's own derived data."""
    # Other doc owns a claim
    other_doc = Document(
        title="Other doc",
        content="content",
        case_id=sample_case.id,
        significance_tier=SignificanceTier.SIGNIFICANT,
    )
    db_session.add(other_doc)
    db_session.flush()

    other_claim = _make_claim(
        db_session,
        asserting_doc=other_doc,
        claim_text="Claim owned by another document",
    )
    db_session.flush()

    # significant_doc has its own ASSERTED claim (will be deleted on retry).
    # Backdate the ASSERTS row so the debounce window doesn't short-circuit
    # extract() before the cleanup branch.
    own_claim = _make_claim(
        db_session,
        asserting_doc=significant_doc,
        claim_text="Own claim from prior extraction that will be cleared on retry",
        asserts_ingest_date=_PRE_DEBOUNCE,
    )
    db_session.flush()

    # significant_doc CONTESTS other_claim — this evidence must survive.
    cross_evidence = ClaimEvidence(
        claim_id=other_claim.id,
        document_id=significant_doc.id,
        role=ClaimEvidenceRole.CONTESTS,
        excerpt="cross-doc evidence",
        confidence=RelationshipConfidence.AI_DETECTED,
    )
    db_session.add(cross_evidence)
    db_session.commit()

    ai_result = {"new_claims": [], "evidence_links": []}

    with (
        patch(
            "app.services.intelligence.claim_extractor.SessionLocal",
            return_value=db_session,
        ),
        patch.object(db_session, "close"),
        patch(
            "app.services.intelligence.claim_extractor._call_claim_extractor_sync",
            return_value=ai_result,
        ),
    ):
        from app.services.intelligence.claim_extractor import extract

        extract(significant_doc.id)

    db_session.expire_all()
    # other_claim must still exist (it was originated by other_doc, not cleared).
    assert db_session.get(Claim, other_claim.id) is not None
    # The cross-doc CONTESTS evidence pointing at other_claim must still
    # exist alongside the original ASSERTS row from other_doc.
    surviving = (
        db_session.query(ClaimEvidence)
        .filter(ClaimEvidence.claim_id == other_claim.id)
        .all()
    )
    surviving_roles = {ev.role for ev in surviving}
    assert ClaimEvidenceRole.CONTESTS in surviving_roles
    assert ClaimEvidenceRole.ASSERTS in surviving_roles
    # significant_doc's own ASSERTED claim was cleared.
    assert db_session.get(Claim, own_claim.id) is None


# ---------------------------------------------------------------------------
# Debounce: defense in depth against the dispatch-race that destroyed doc_39's
# 3-claim extraction (cron fired extract_claims_task multiple times in 8 min;
# the second/third runs deleted prior good claims via stale-cleanup, and when
# they then failed to produce JSON the original work was gone).
# ---------------------------------------------------------------------------


@pytest.mark.unit
def test_extract_debounces_when_recent_asserts_exist(db_session, significant_doc):
    """If a successful extraction wrote ASSERTS rows for this doc inside the
    _RECENT_EXTRACTION_WINDOW_SECS window, extract() must return the
    'recent_extraction' skip reason without entering the destructive
    stale-cleanup branch — leaving the prior result intact."""
    from app.services.intelligence.claim_extractor import extract

    # Seed a successful prior extraction: a Claim with an ASSERTS row owned
    # by significant_doc, dated "just now". This mirrors what a normal
    # extract() run would have written.
    prior = _make_claim(
        db_session,
        asserting_doc=significant_doc,
        claim_text="A claim from a recent prior run",
    )
    db_session.commit()
    prior_id = prior.id

    called = {"ai": 0}

    def fake_ai(*_a, **_k):
        called["ai"] += 1
        return {"new_claims": [], "evidence_links": []}

    with (
        patch(
            "app.services.intelligence.claim_extractor.SessionLocal",
            return_value=db_session,
        ),
        patch.object(db_session, "close"),
        patch(
            "app.services.intelligence.claim_extractor._call_claim_extractor_sync",
            side_effect=fake_ai,
        ),
    ):
        result = extract(significant_doc.id)

    assert result == "recent_extraction", f"expected debounce skip, got {result!r}"
    assert called["ai"] == 0, "AI must not be called inside the debounce window"
    # Prior claim survives — debounce ran BEFORE the destructive stale-cleanup.
    assert db_session.get(Claim, prior_id) is not None


@pytest.mark.unit
def test_extract_proceeds_when_no_recent_asserts(db_session, significant_doc):
    """Counter-test: with no ASSERTS rows for this doc inside the window,
    extract() proceeds normally."""
    from app.services.intelligence.claim_extractor import extract

    ai_result = {
        "new_claims": [
            {
                "claim_text": "Fresh extraction claim",
                "claim_type": "factual",
                "excerpt": "n/a",
            }
        ],
        "evidence_links": [],
    }

    with (
        patch(
            "app.services.intelligence.claim_extractor.SessionLocal",
            return_value=db_session,
        ),
        patch.object(db_session, "close"),
        patch(
            "app.services.intelligence.claim_extractor._call_claim_extractor_sync",
            return_value=ai_result,
        ),
    ):
        result = extract(significant_doc.id)

    assert result is None, "extract() should have run, not returned a skip reason"
