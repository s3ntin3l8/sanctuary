"""Dates that reach the AI or an agent are the reader's calendar, not UTC's."""

from datetime import date, datetime
from zoneinfo import ZoneInfo

import pytest

from app.models.database import Claim, ClaimEvidence, Document
from app.models.enums import ClaimEvidenceRole, ClaimStatus
from app.services import timezone_service

LATE_UTC = datetime(2026, 6, 1, 23, 30)  # 01:30 the next day in Berlin (CEST)
MIDNIGHT = datetime(2026, 6, 1)  # a printed letter date: never shifted


@pytest.fixture
def berlin(monkeypatch):
    monkeypatch.setattr(timezone_service, "_cached_tz", ZoneInfo("Europe/Berlin"))


@pytest.mark.unit
def test_local_date_or_rolls_over_and_keeps_midnight(berlin):
    assert timezone_service.local_date_or(LATE_UTC) == "2026-06-02"
    assert timezone_service.local_date_or(MIDNIGHT) == "2026-06-01"
    assert timezone_service.local_date_or(LATE_UTC, fmt="%d.%m.%Y") == "02.06.2026"
    assert timezone_service.local_date_or(None) == "unknown"
    assert timezone_service.local_date_or(None, "-") == "-"


@pytest.mark.unit
def test_mcp_iso_date_is_local_and_leaves_plain_dates(berlin):
    from app.mcp.serializers import iso_date

    assert iso_date(LATE_UTC) == "2026-06-02"
    assert iso_date(MIDNIGHT) == "2026-06-01"
    assert iso_date(date(2026, 6, 1)) == "2026-06-01"
    assert iso_date(None) is None


@pytest.mark.unit
def test_retrieval_hit_date_is_local(berlin):
    from app.services.chat.retrieval import _to_hit

    doc = Document(id=1, title="t", issued_date=LATE_UTC)
    assert _to_hit(doc, []).issued_date == "2026-06-02"
    assert _to_hit(Document(id=2, title="t"), []).issued_date is None


@pytest.mark.unit
def test_relationship_candidate_date_is_local(berlin):
    from app.services.intelligence import relationship_detector as rd

    doc = Document(id=3, title="t", issued_date=LATE_UTC)
    line = rd._build_candidate_summary(rd._Candidate(doc, frozenset({"topic"})))
    assert "Date=2026-06-02 |" in line
    undated = Document(id=4, title="t")
    assert "Date=unknown |" in rd._build_candidate_summary(
        rd._Candidate(undated, frozenset({"topic"}))
    )


@pytest.mark.unit
def test_document_chat_prompt_keeps_german_format_in_local_time(
    berlin, db_session, sample_case
):
    from app.services.chat.context_builder import build_document_chat_prompt

    doc = Document(
        title="Brief", content="x", case_id=sample_case.id, issued_date=LATE_UTC
    )
    db_session.add(doc)
    db_session.commit()

    prompt = build_document_chat_prompt(doc, db_session, [], "Frage")
    assert "Issued: 02.06.2026" in prompt


@pytest.mark.unit
def test_claim_anchor_date_is_local(berlin, db_session, sample_case):
    from app.services.intelligence.claim_context import format_claims_for_case

    doc = Document(title="Src", content="x", case_id=sample_case.id)
    db_session.add(doc)
    db_session.flush()
    claim = Claim(
        claim_text="Fact",
        status=ClaimStatus.ASSERTED,
        first_made_at=LATE_UTC,
        last_updated_at=LATE_UTC,
    )
    db_session.add(claim)
    db_session.flush()
    db_session.add(
        ClaimEvidence(
            claim_id=claim.id, document_id=doc.id, role=ClaimEvidenceRole.ASSERTS
        )
    )
    db_session.commit()

    assert "first made 2026-06-02" in format_claims_for_case(db_session, sample_case.id)


@pytest.mark.unit
def test_calendar_times_use_the_configured_timezone(berlin, monkeypatch):
    from app.services.ingestion.calendar_text import _when

    assert _when("20260601T233000Z", {}) == "02.06.2026 01:30"
    monkeypatch.setattr(timezone_service, "_cached_tz", ZoneInfo("Asia/Tokyo"))
    assert _when("20260601T233000Z", {}) == "02.06.2026 08:30"
    assert _when("20260601T100000", {}) == "01.06.2026 10:00"  # floating = local
