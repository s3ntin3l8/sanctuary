"""A misread issue date shows up against the bundle it sits in."""

from datetime import UTC, datetime

import pytest

from app.models.database import Document, IngestBatch, User
from app.models.enums import (
    DocumentRole,
    IngestBatchSourceType,
    IngestBatchStatus,
    OriginatorType,
)
from app.services.ingestion.plausibility import issued_date_suspect
from app.services.ingestion.service import compute_review_reasons


def _day(y, m, d):
    return datetime(y, m, d, tzinfo=UTC)


@pytest.fixture
def bundle(db_session):
    """A cover letter issued 2026-08-28 and one enclosure, scanned 2026-10-09."""
    owner = db_session.query(User).filter_by(email="admin@localhost").one()
    batch = IngestBatch(
        owner_id=owner.id,
        source_type=IngestBatchSourceType.SCAN,
        status=IngestBatchStatus.PROCESSING,
        received_at=_day(2026, 10, 9),
    )
    db_session.add(batch)
    db_session.flush()
    lead = Document(
        title="Antrag",
        owner_id=owner.id,
        case_id="_TRIAGE",
        ingest_batch_id=batch.id,
        role=DocumentRole.COVER_LETTER,
        issued_date=_day(2026, 8, 28),
        originator_type=OriginatorType.OWN,
    )
    db_session.add(lead)
    db_session.flush()
    enclosure = Document(
        title="Erklärung",
        owner_id=owner.id,
        case_id="_TRIAGE",
        ingest_batch_id=batch.id,
        role=DocumentRole.ENCLOSURE,
        parent_id=lead.id,
        issued_date=_day(2026, 8, 28),
        originator_type=OriginatorType.OWN,
    )
    db_session.add(enclosure)
    db_session.commit()
    return lead, enclosure


@pytest.mark.integration
def test_a_year_misread_by_one_digit_is_suspect(db_session, bundle):
    _, enclosure = bundle
    enclosure.issued_date = _day(2016, 8, 28)

    assert issued_date_suspect(enclosure, db_session)
    assert "issued_date_suspect" in compute_review_reasons(enclosure)


@pytest.mark.integration
def test_the_cover_letters_own_date_and_an_unrelated_older_date_are_fine(
    db_session, bundle
):
    lead, enclosure = bundle
    assert not issued_date_suspect(enclosure, db_session)
    # old enclosures are normal; only the same-day-different-year pattern is suspect
    enclosure.issued_date = _day(2024, 3, 2)
    assert not issued_date_suspect(enclosure, db_session)
    assert not issued_date_suspect(lead, db_session)


@pytest.mark.integration
def test_a_letter_issued_after_it_arrived_is_suspect(db_session, bundle):
    lead, _ = bundle
    lead.issued_date = _day(2027, 8, 28)
    assert issued_date_suspect(lead, db_session)
    # a day of slack for time zones
    lead.issued_date = _day(2026, 10, 10)
    assert not issued_date_suspect(lead, db_session)
    lead.received_date = _day(2026, 8, 20)
    lead.issued_date = _day(2026, 8, 28)
    assert issued_date_suspect(lead, db_session)


@pytest.mark.integration
def test_a_date_the_user_set_is_never_second_guessed(db_session, bundle):
    _, enclosure = bundle
    enclosure.issued_date = _day(2016, 8, 28)
    enclosure.extraction_confidence = {"issued_date": "user_set"}
    assert not issued_date_suspect(enclosure, db_session)


@pytest.mark.integration
def test_a_document_without_a_date_or_bundle_is_not_suspect(db_session):
    lone = Document(title="x", case_id="_TRIAGE", originator_type=OriginatorType.OWN)
    db_session.add(lone)
    db_session.commit()
    assert not issued_date_suspect(lone, db_session)
    lone.issued_date = _day(2016, 8, 28)
    assert not issued_date_suspect(lone, db_session)


@pytest.mark.integration
def test_batch_analysis_raises_the_reason_once_the_bundle_is_complete(
    db_session, bundle
):
    """The check needs the cover letter's date, so it lands after batch analysis."""
    from unittest.mock import patch

    from app.tasks.analyze_batch import analyze_batch_task

    lead, enclosure = bundle
    enclosure.issued_date = _day(2016, 8, 28)
    enclosure.review_reasons = []
    db_session.commit()

    with (
        patch("app.config.SessionLocal", return_value=db_session),
        patch.object(db_session, "close"),
        patch("app.services.intelligence.batch_analyzer.analyze", return_value=False),
        patch("app.tasks.analyze_batch._enrich_if_pending"),
    ):
        analyze_batch_task.run(enclosure.ingest_batch_id)

    db_session.refresh(enclosure)
    db_session.refresh(lead)
    assert "issued_date_suspect" in enclosure.review_reasons
    assert enclosure.needs_review
    assert "issued_date_suspect" not in lead.review_reasons


@pytest.mark.integration
def test_a_date_of_birth_far_older_than_the_bundle_is_suspect(db_session, bundle):
    # a party's birth date picked off the parties block (1986) or a century misread
    _, enclosure = bundle
    for year in (1986, 2016, 2020):
        enclosure.issued_date = _day(year, 9, 12)
        assert issued_date_suspect(enclosure, db_session), year


@pytest.mark.integration
def test_a_modestly_older_enclosure_is_not_suspect(db_session, bundle):
    _, enclosure = bundle
    enclosure.issued_date = _day(2023, 9, 4)  # e.g. a bank letter enclosed as proof
    assert not issued_date_suspect(enclosure, db_session)


@pytest.mark.integration
def test_a_scans_arrival_day_says_nothing_about_how_old_a_letter_may_be(
    db_session, bundle
):
    """An old letter scanned today is not suspect for being old; the same date in
    an email bundle (arrival = when it was sent) is."""
    lead, enclosure = bundle
    lead.issued_date = None  # no cover-letter date to compare with
    enclosure.issued_date = _day(2015, 3, 4)
    db_session.commit()

    assert not issued_date_suspect(enclosure, db_session)  # SCAN bundle

    batch = db_session.get(IngestBatch, enclosure.ingest_batch_id)
    batch.source_type = IngestBatchSourceType.EMAIL
    db_session.commit()
    assert issued_date_suspect(enclosure, db_session)
