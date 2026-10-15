"""The user's note about an email's attachments reports their claims, it does not originate them."""

from datetime import datetime, timedelta
from unittest.mock import patch

import pytest

from app.core.timezone import now_utc
from app.models.database import (
    Claim,
    ClaimEvidence,
    ClaimEvidenceProposal,
    ClaimMergeProposal,
    Document,
    IngestBatch,
)
from app.models.enums import (
    ClaimEvidenceRole,
    ClaimStatus,
    ClaimType,
    IngestBatchSourceType,
    IngestBatchStatus,
    OriginatorType,
    ProposalConfidence,
    ProposalStatus,
    RelationshipConfidence,
    SignificanceTier,
    stronger_role,
)
from app.services.claim_proposal_service import confirm_evidence, confirm_merge

pytestmark = pytest.mark.unit

R = ClaimEvidenceRole


@pytest.fixture
def mail(db_session, sample_case):
    """An own-mail body and the court packet attached to it, in one batch."""
    batch = IngestBatch(
        source_type=IngestBatchSourceType.EMAIL, status=IngestBatchStatus.PROCESSING
    )
    db_session.add(batch)
    db_session.flush()
    body = Document(
        title="Anweisung",
        content="Sie behauptet, ich hätte sie unter Druck gesetzt.",
        case_id=sample_case.id,
        ingest_batch_id=batch.id,
        originator_type=OriginatorType.OWN,
        significance_tier=SignificanceTier.SIGNIFICANT,
        original_filename=f"email_body_{batch.id}.txt",
    )
    filing = Document(
        title="Antragsschrift",
        content="Die Antragsgegnerin behauptet, der Antragsteller setze sie unter Druck.",
        case_id=sample_case.id,
        ingest_batch_id=batch.id,
        originator_type=OriginatorType.OPPOSING,
        significance_tier=SignificanceTier.SIGNIFICANT,
        original_filename="packet.pdf",
    )
    db_session.add_all([body, filing])
    db_session.commit()
    return db_session, batch, body, filing


def _claim(db, text, *, age_minutes=0):
    claim = Claim(
        claim_text=text,
        claim_type=ClaimType.FACTUAL,
        status=ClaimStatus.ASSERTED,
        first_made_at=datetime.now(),
        last_updated_at=now_utc() - timedelta(minutes=age_minutes),
    )
    db.add(claim)
    db.flush()
    return claim


def _asserts(db, claim, doc, confidence=RelationshipConfidence.AI_DETECTED):
    db.add(
        ClaimEvidence(
            claim_id=claim.id,
            document_id=doc.id,
            role=R.ASSERTS,
            confidence=confidence,
            # Older than the extractor's debounce window.
            ingest_date=now_utc() - timedelta(hours=1),
        )
    )
    db.flush()


def _merge(db, absorbed, survivor):
    prop = ClaimMergeProposal(
        new_claim_id=absorbed.id,
        existing_claim_id=survivor.id,
        confidence=ProposalConfidence.HIGH,
        status=ProposalStatus.PENDING,
        proposed_at=now_utc(),
    )
    db.add(prop)
    db.flush()
    confirm_merge(prop.id, db)
    db.commit()


def _roles(db, claim_id):
    return {
        (e.document_id, e.role)
        for e in db.query(ClaimEvidence).filter(ClaimEvidence.claim_id == claim_id)
    }


def test_reports_is_the_weakest_role():
    assert stronger_role(R.REPORTS, R.CITES_AS_PROOF) == R.CITES_AS_PROOF
    assert stronger_role(R.ASSERTS, R.REPORTS) == R.ASSERTS


@pytest.mark.parametrize("body_claim_survives", [True, False])
def test_merging_the_notes_claim_with_the_filings_makes_the_note_a_reporter(
    mail, body_claim_survives
):
    db, _batch, body, filing = mail
    from_note = _claim(db, "Liu behauptet, sie stehe unter Druck.")
    from_filing = _claim(db, "Die Antragsgegnerin steht unter Druck.")
    _asserts(db, from_note, body)
    _asserts(db, from_filing, filing)
    survivor, absorbed = (
        (from_note, from_filing) if body_claim_survives else (from_filing, from_note)
    )

    _merge(db, absorbed, survivor)

    assert _roles(db, survivor.id) == {(body.id, R.REPORTS), (filing.id, R.ASSERTS)}


def test_a_note_nobody_else_asserts_keeps_its_claim(mail):
    from app.services.claim_proposal_service import demote_reported_assertions

    db, _batch, body, _filing = mail
    alone = _claim(db, "Ich habe Unterhalt vollstreckt.")
    _asserts(db, alone, body)

    demote_reported_assertions(db, alone)

    assert _roles(db, alone.id) == {(body.id, R.ASSERTS)}


def test_a_row_a_person_confirmed_is_not_demoted(mail):
    db, _batch, body, filing = mail
    a = _claim(db, "Liu behauptet, sie stehe unter Druck.")
    b = _claim(db, "Die Antragsgegnerin steht unter Druck.")
    _asserts(db, a, body, RelationshipConfidence.USER_CONFIRMED)
    _asserts(db, b, filing)

    _merge(db, b, a)

    assert (body.id, R.ASSERTS) in _roles(db, a.id)


def test_two_filings_asserting_one_claim_both_keep_asserts(mail):
    db, batch, _body, filing = mail
    second = Document(
        title="Zweite",
        content="x",
        case_id=filing.case_id,
        ingest_batch_id=batch.id,
        originator_type=OriginatorType.OPPOSING,
        original_filename="second.pdf",
    )
    db.add(second)
    db.flush()
    a, b = _claim(db, "Wohnsitz in Hepberg."), _claim(db, "Sie wohnt in Hepberg.")
    _asserts(db, a, filing)
    _asserts(db, b, second)

    _merge(db, b, a)

    assert _roles(db, a.id) == {(filing.id, R.ASSERTS), (second.id, R.ASSERTS)}


def test_a_confirmed_reports_suggestion_becomes_evidence(mail):
    db, _batch, body, filing = mail
    claim = _claim(db, "Die Antragsgegnerin steht unter Druck.")
    _asserts(db, claim, filing)
    prop = ClaimEvidenceProposal(
        target_claim_id=claim.id,
        source_document_id=body.id,
        proposed_role=R.REPORTS,
        excerpt="Sie behauptet, ich hätte sie unter Druck gesetzt.",
        confidence=ProposalConfidence.MEDIUM,
        status=ProposalStatus.PENDING,
        proposed_at=now_utc(),
    )
    db.add(prop)
    db.flush()

    confirm_evidence(prop.id, db)
    db.commit()

    assert _roles(db, claim.id) == {(filing.id, R.ASSERTS), (body.id, R.REPORTS)}


# --- the extractor ------------------------------------------------------------


def _extract(db, doc_id):
    from app.services.intelligence import claim_extractor

    calls = []

    def fake(doc, existing, **kw):
        calls.append((list(existing), kw))
        return {"new_claims": [], "evidence_links": []}

    with (
        patch("app.config.SessionLocal", return_value=db),
        patch.object(db, "close"),
        patch.object(claim_extractor, "_call_claim_extractor_sync", side_effect=fake),
    ):
        claim_extractor.extract(doc_id)
    return calls


def test_the_note_is_matched_against_its_attachments_claims_first(mail):
    db, _batch, body, filing = mail
    ours = _claim(db, "Die Antragsgegnerin steht unter Druck.", age_minutes=60)
    _asserts(db, ours, filing)
    newer = _claim(db, "Ein jüngerer Anspruch aus einem anderen Schreiben.")
    unrelated = Document(
        title="Anderes", content="x", case_id=body.case_id, significance_tier=None
    )
    db.add(unrelated)
    db.flush()
    _asserts(db, newer, unrelated)
    db.commit()

    ((existing, kw),) = _extract(db, body.id)

    assert existing[0].id == ours.id  # the attachment's claim leads, though older
    assert kw["reports_note"] is True


def test_a_note_without_attachment_claims_gets_no_reports_hint(mail):
    db, _batch, body, _filing = mail

    ((existing, kw),) = _extract(db, body.id)

    assert kw["reports_note"] is False


def test_an_ordinary_document_gets_no_reports_hint(mail):
    db, _batch, _body, filing = mail
    other = _claim(db, "Irgendwas.")
    _asserts(db, other, filing)
    db.commit()

    ((existing, kw),) = _extract(db, filing.id)

    assert kw["reports_note"] is False


def test_the_prompt_asks_for_reports_only_when_told_to(mail):
    from app.services.intelligence import claim_extractor
    from app.services.intelligence.schemas import ClaimExtraction

    db, _batch, body, _filing = mail
    seen = []

    def fake(**kwargs):
        seen.append(kwargs["user_prompt"])
        return ClaimExtraction()

    with patch.object(claim_extractor, "call_json_ai", side_effect=fake):
        claim_extractor._call_claim_extractor_sync(body, [], reports_note=True)
        claim_extractor._call_claim_extractor_sync(body, [], reports_note=False)

    assert 'role "reports"' in seen[0]
    assert "reports" not in seen[1]
